"""Tests for stuck detection, the control lease, and the handoff.

The behaviour worth pinning here is not "a file gets written". It is that
automation genuinely cannot act while a human holds the session, that the run
notices when the human hands it back, and that a run left unattended fails with
a reason rather than blocking forever.
"""

from datetime import UTC, datetime

import pytest

from src.escalation.conditions import OPERATOR_GUIDANCE, StuckCondition
from src.escalation.intervention import (
    HumanAction,
    Intervention,
    InterventionTimeout,
    make_intervention_id,
    pending_interventions,
    request_from_stuck,
)
from src.session.lease import (
    UNCLAIMED,
    ControlDeniedError,
    LeaseConflictError,
    LeaseOwner,
    LeaseStore,
)


def make_request(condition=StuckCondition.LOCATOR_UNRESOLVED, step_index=3):
    return request_from_stuck(
        condition,
        run_id="20260908T230000Z-abcd",
        step_index=step_index,
        goal="look up member 12345 and read their savings balance",
        step_intent="click the Open Record link",
        url="http://localhost:5000/search",
        title="Search",
        detail="no link named 'Open Record' in the results frame",
    )


# --------------------------------------------------------------------------- #
# Stuck conditions
# --------------------------------------------------------------------------- #
def test_stuck_is_enumerated_not_a_single_max_steps_case():
    # The requirement is >= 6 distinct conditions. The point of the number is
    # that "the run stopped" is not one situation for an operator to handle.
    assert len(StuckCondition) >= 6


def test_every_condition_tells_the_operator_what_to_do():
    # A condition with no guidance is an enum member that makes a human do the
    # classification work the system was supposed to do.
    missing = [c for c in StuckCondition if not OPERATOR_GUIDANCE.get(c, "").strip()]
    assert missing == []


def test_a_request_carries_the_context_an_operator_needs():
    request = make_request()

    assert request.condition is StuckCondition.LOCATOR_UNRESOLVED
    assert request.guidance == OPERATOR_GUIDANCE[StuckCondition.LOCATOR_UNRESOLVED]
    assert request.step_index == 3
    assert request.goal.startswith("look up member")
    assert request.url == "http://localhost:5000/search"
    assert "Open Record" in request.detail


def test_intervention_ids_are_unique_per_step_so_one_run_can_escalate_twice():
    assert make_intervention_id("run-1", 3) != make_intervention_id("run-1", 7)


# --------------------------------------------------------------------------- #
# The lease
# --------------------------------------------------------------------------- #
def test_automation_holds_control_until_something_takes_it(tmp_path):
    store = LeaseStore(tmp_path / "lease.json")

    assert store.read().owner is LeaseOwner.AUTOMATION
    store.require_automation()  # does not raise


def test_automation_cannot_act_while_a_human_holds_the_lease(tmp_path):
    store = LeaseStore(tmp_path / "lease.json")
    store.acquire(LeaseOwner.HUMAN, "operator-1", reason="taking a look")

    with pytest.raises(ControlDeniedError) as error:
        store.require_automation()

    # The message has to name who and why, or the run's log says only "denied".
    assert "operator-1" in str(error.value)
    assert "taking a look" in str(error.value)


def test_a_second_operator_is_refused_rather_than_silently_taking_over(tmp_path):
    store = LeaseStore(tmp_path / "lease.json")
    store.acquire(LeaseOwner.HUMAN, "operator-1")

    with pytest.raises(LeaseConflictError):
        store.acquire(LeaseOwner.HUMAN, "operator-2")


def test_the_same_operator_can_re_take_their_own_lease(tmp_path):
    store = LeaseStore(tmp_path / "lease.json")
    store.acquire(LeaseOwner.HUMAN, "operator-1")

    lease = store.acquire(LeaseOwner.HUMAN, "operator-1")

    assert lease.holder_id == "operator-1"


# --------------------------------------------------------------------------- #
# The handoff
# --------------------------------------------------------------------------- #
def test_raising_an_intervention_stops_automation_acting(tmp_path):
    intervention = Intervention("i-1", root=tmp_path)

    intervention.raise_request(make_request())

    assert intervention.request_path.exists()
    assert intervention.lease.read().owner is LeaseOwner.HUMAN
    with pytest.raises(ControlDeniedError):
        intervention.lease.require_automation()


def test_the_request_is_readable_back_by_the_operator(tmp_path):
    intervention = Intervention("i-2", root=tmp_path)
    intervention.raise_request(make_request(step_index=5))

    reloaded = Intervention("i-2", root=tmp_path).read_request()

    assert reloaded.step_index == 5
    assert reloaded.condition is StuckCondition.LOCATOR_UNRESOLVED


def test_resume_returns_control_and_the_run_stops_waiting(tmp_path):
    intervention = Intervention("i-3", root=tmp_path)
    intervention.raise_request(make_request())
    intervention.take("operator-1")

    # The operator resumes on the second poll, so this also proves the run is
    # actually looping rather than reading the lease once.
    ticks = iter([0.0, 1.0, 2.0, 3.0, 4.0])
    polls = {"n": 0}

    def sleep(_seconds):
        polls["n"] += 1
        if polls["n"] == 2:
            intervention.resume("operator-1")

    lease = intervention.wait_for_resume(
        timeout_s=100.0, sleep=sleep, now=lambda: next(ticks)
    )

    assert lease.owner is LeaseOwner.AUTOMATION
    assert polls["n"] == 2
    intervention.lease.require_automation()  # automation may act again


def test_an_unattended_run_times_out_with_a_reason(tmp_path):
    intervention = Intervention("i-4", root=tmp_path)
    intervention.raise_request(make_request())

    ticks = iter([0.0, 1.0, 999.0])

    with pytest.raises(InterventionTimeout) as error:
        intervention.wait_for_resume(
            timeout_s=10.0, sleep=lambda _s: None, now=lambda: next(ticks)
        )

    assert "i-4" in str(error.value)


def test_resolved_interventions_drop_off_the_pending_list(tmp_path):
    waiting = Intervention("i-5", root=tmp_path)
    waiting.raise_request(make_request())
    done = Intervention("i-6", root=tmp_path)
    done.raise_request(make_request())
    done.resume("operator-1")

    pending = [i.intervention_id for i in pending_interventions(root=tmp_path)]

    assert pending == ["i-5"]


def test_what_the_human_did_is_recorded_alongside_the_request(tmp_path):
    intervention = Intervention("i-7", root=tmp_path)
    intervention.raise_request(make_request())

    intervention.write_human_actions(
        [
            HumanAction(at=datetime.now(UTC), kind="navigate", url="http://localhost:5000/search"),
            HumanAction(at=datetime.now(UTC), kind="click", role="link", name="Open Record"),
        ]
    )

    lines = intervention.actions_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert "Open Record" in lines[1]


def test_an_unclaimed_human_lease_still_denies_automation(tmp_path):
    # The window between raising an intervention and an operator arriving is a
    # real state, and it is the one where a run must already have stopped.
    intervention = Intervention("i-8", root=tmp_path)
    intervention.raise_request(make_request())

    assert intervention.lease.read().holder_id == UNCLAIMED
    with pytest.raises(ControlDeniedError):
        intervention.lease.require_automation()


def test_an_operator_can_claim_an_unclaimed_lease(tmp_path):
    # Regression: the placeholder holder was once treated as a rival operator,
    # so the first person to run `take` was refused by the intervention itself.
    intervention = Intervention("i-9", root=tmp_path)
    intervention.raise_request(make_request())

    lease = intervention.take("operator-1")

    assert lease.holder_id == "operator-1"
