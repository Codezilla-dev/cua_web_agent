"""The run, explained to a person.

`trace.jsonl` is the evidence and is the right shape for an auditor with a
question. It is the wrong shape for "so what happened?", which is the question
almost everyone actually has -- and `Trace.summary` was declared in `types.py`
and never assigned by anything, so `run.json` carried `"summary": null` on every
run ever produced.

The property that matters most here is that this is written **whatever the
outcome**. The runs worth explaining are mostly the ones that did not reach
their goal, so every one of them gets a test.
"""

from datetime import UTC, datetime, timedelta

import pytest

from src.summary import one_line, render
from src.types import (
    ActionKind,
    ActResult,
    Decision,
    Expectation,
    Intent,
    PerceptionCoverage,
    PolicyVerdict,
    RiskTier,
    RunOutcome,
    StepOutcome,
    StepRecord,
    TextVisibleCheck,
    Trace,
    UIState,
    VerifyResult,
)

START = datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC)


def state(url: str = "http://localhost:5000/search") -> UIState:
    return UIState(
        url=url,
        title="page",
        elements=[],
        text_digest="digest",
        coverage=PerceptionCoverage(ok=True, interactive_count=1, unnamed_ratio=0.0),
        captured_at=START,
    )


def step(
    index: int,
    action: ActionKind = ActionKind.CLICK,
    *,
    target_description: str = "Find Member button",
    value: str | None = None,
    intent_label: str = "click find",
    outcome: StepOutcome = StepOutcome.OK,
    act_ok: bool = True,
    act_error: str | None = None,
    read_value: str | None = None,
    verify_ok: bool | None = True,
    check_performed: str = "looked for '/search' in the URL",
    evidence: str = "URL was '/search'",
    halted: bool = False,
    recovery_note: str | None = None,
) -> StepRecord:
    return StepRecord(
        index=index,
        started_at=START,
        duration_ms=10,
        state_before=state(),
        decision=Decision(
            intent=Intent(
                reasoning="because",
                intent=intent_label,
                action=action,
                target_description=target_description,
                value=value,
                risk=RiskTier.SAFE,
                expectation=Expectation(
                    description="d", check=TextVisibleCheck(kind="text_visible", text="x")
                ),
            ),
            model="test",
            latency_ms=1,
        ),
        policy=PolicyVerdict(
            allowed=not halted,
            tier=RiskTier.IRREVERSIBLE if halted else RiskTier.SAFE,
            rule="risk.irreversible" if halted else "allow",
            reason="target 'Confirm Transfer' matches the irreversible pattern"
            if halted
            else "allowed",
            disposition="halt" if halted else "allow",
        ),
        act=None
        if halted
        else ActResult(
            ok=act_ok, action=action, duration_ms=1, error=act_error, read_value=read_value
        ),
        verify=None
        if verify_ok is None
        else VerifyResult(
            ok=verify_ok,
            expectation=Expectation(
                description="d", check=TextVisibleCheck(kind="text_visible", text="x")
            ),
            check_performed=check_performed,
            evidence=evidence,
        ),
        outcome=outcome,
        recovery_note=recovery_note,
    )


def trace_of(steps, outcome: RunOutcome, error: str | None = None) -> Trace:
    return Trace(
        run_id="20260909T120000Z-test",
        goal="look up member 12345 and read their savings balance",
        target_url="http://localhost:5000",
        started_at=START,
        finished_at=START + timedelta(seconds=48),
        outcome=outcome,
        steps=steps,
        error=error,
    )


# --- written for every outcome ----------------------------------------------
@pytest.mark.parametrize(
    "outcome",
    [
        RunOutcome.COMPLETED,
        RunOutcome.DEAD_END,
        RunOutcome.HALTED_POLICY,
        RunOutcome.BUDGET_EXCEEDED,
        RunOutcome.ERROR,
    ],
)
def test_every_outcome_produces_a_summary_and_a_reason(outcome):
    # The runs worth explaining are mostly the ones that failed, so none of
    # these may fall through to an empty or generic account of itself.
    document = render(trace_of([step(0)], outcome, error="boom"))

    assert document.startswith("# look up member 12345")
    assert "## Why it stopped" in document
    reason = document.split("## Why it stopped", 1)[1].strip()
    assert len(reason) > 40, f"{outcome} has no real explanation"


def test_a_run_with_no_steps_at_all_still_renders():
    # The surface failed to start, or the budget was zero. This is exactly when
    # a person most needs to be told something.
    document = render(trace_of([], RunOutcome.ERROR, error="no browser available"))

    assert "the run ended before any step ran" in document
    assert "no browser available" in document


def test_an_unfinished_run_does_not_claim_a_duration():
    unfinished = trace_of([step(0)], RunOutcome.ERROR).model_copy(
        update={"finished_at": None}
    )

    assert "unknown (the run did not finish)" in render(unfinished)


# --- the one-liner ----------------------------------------------------------
def test_a_completed_run_names_what_it_read():
    steps = [
        step(0),
        step(1, ActionKind.READ, target_description="savings balance", read_value="$4,812.55"),
    ]

    line = one_line(trace_of(steps, RunOutcome.COMPLETED))

    assert line.startswith("Completed after 2 steps")
    assert "savings balance" in line and "$4,812.55" in line


def test_a_completed_run_that_read_nothing_says_so():
    line = one_line(trace_of([step(0)], RunOutcome.COMPLETED))

    assert "reading no values" in line


def test_a_dead_end_names_the_step_and_what_it_expected():
    failed = step(
        2,
        intent_label="submit login",
        verify_ok=False,
        check_performed="looked for 'overview.htm' in the URL",
        evidence="URL was 'login.htm'",
    )

    line = one_line(trace_of([step(0), step(1), failed], RunOutcome.DEAD_END))

    assert "submit login" in line
    assert "overview.htm" in line and "login.htm" in line


def test_a_policy_halt_names_the_rule_rather_than_the_enum():
    halted = step(1, intent_label="click Confirm Transfer", halted=True, verify_ok=None,
                  outcome=StepOutcome.HALTED)

    line = one_line(trace_of([step(0), halted], RunOutcome.HALTED_POLICY))

    assert "risk.irreversible" in line
    assert "Confirm Transfer" in line


def test_an_errored_run_carries_the_error():
    line = one_line(trace_of([step(0)], RunOutcome.ERROR, error="planner could not parse"))

    assert "planner could not parse" in line


def test_a_budget_exceeded_run_says_which_budget():
    line = one_line(trace_of([step(0)], RunOutcome.BUDGET_EXCEEDED))

    assert "budget" in line.lower()


def test_one_step_is_not_pluralised():
    assert "after 1 step," in one_line(trace_of([step(0)], RunOutcome.COMPLETED))


# --- credentials ------------------------------------------------------------
def test_a_value_typed_into_a_credential_field_is_never_printed():
    # The same `is_unobservable_field` rule the redactor and the compiler use,
    # so all three agree about what counts as a secret.
    typed = step(
        0,
        ActionKind.TYPE,
        target_description="Passcode textbox",
        value="vault-echo-77-quill",
        intent_label="type the passcode",
    )

    document = render(trace_of([typed], RunOutcome.COMPLETED))

    assert "vault-echo-77-quill" not in document
    assert "(not recorded)" in document


def test_an_ordinary_typed_value_is_shown_because_it_is_what_makes_this_readable():
    typed = step(
        0, ActionKind.TYPE, target_description="Member ID or Name", value="22841"
    )

    assert "22841" in render(trace_of([typed], RunOutcome.COMPLETED))


# --- the readable body ------------------------------------------------------
def test_steps_are_numbered_as_the_trace_numbers_them():
    # A line here and a line in trace.jsonl must mean the same step, or
    # cross-referencing the two is worse than useless.
    document = render(trace_of([step(0), step(1)], RunOutcome.COMPLETED))

    assert "\n0. " in document and "\n1. " in document


def test_a_failed_step_shows_what_was_expected_and_what_was_seen():
    failed = step(0, verify_ok=False, check_performed="looked for X", evidence="saw Y")

    document = render(trace_of([failed], RunOutcome.DEAD_END))

    assert "expected: looked for X" in document
    assert "observed: saw Y" in document


def test_a_halted_step_shows_the_rule_that_stopped_it():
    halted = step(0, halted=True, verify_ok=None, outcome=StepOutcome.HALTED)

    document = render(trace_of([halted], RunOutcome.HALTED_POLICY))

    assert "stopped by policy" in document
    assert "risk.irreversible" in document


def test_an_action_that_would_not_run_is_reported_as_such():
    broken = step(0, act_ok=False, act_error="element was detached", verify_ok=None)

    document = render(trace_of([broken], RunOutcome.DEAD_END))

    assert "the action did not run: element was detached" in document


def test_recoveries_are_listed_where_they_happened():
    retried = step(1, recovery_note="retry after: expectation not met")

    document = render(trace_of([step(0), retried], RunOutcome.COMPLETED))

    assert "## Where it had to recover" in document
    assert "step 1: retry after" in document


def test_a_run_that_recovered_nothing_has_no_recovery_section():
    document = render(trace_of([step(0)], RunOutcome.COMPLETED))

    assert "## Where it had to recover" not in document


def test_read_values_are_reported_as_outputs():
    reader = step(0, ActionKind.READ, target_description="savings balance",
                  read_value="$918.40")

    document = render(trace_of([reader], RunOutcome.COMPLETED))

    assert "savings balance" in document and "$918.40" in document


def test_a_run_that_read_nothing_says_so_rather_than_leaving_the_section_empty():
    document = render(trace_of([step(0)], RunOutcome.DEAD_END))

    assert "Nothing was read" in document


def test_the_summary_is_plain_text_with_no_json_or_refs():
    # The whole point is that it is not the trace. A ref like `e7` or a raw
    # enum name leaking in means someone rendered a field instead of a fact.
    document = render(trace_of([step(0), step(1, ActionKind.READ, read_value="$1")],
                               RunOutcome.COMPLETED))

    assert "{" not in document and "}" not in document
    assert "StepOutcome." not in document and "RunOutcome." not in document


def test_a_trace_with_no_outcome_recorded_says_so():
    # `outcome` is assigned as the run ends, so a crash between the last step
    # and that assignment leaves it unset. A summary implying the run finished
    # tidily would be worse than none.
    unfinished = trace_of([step(0)], RunOutcome.COMPLETED).model_copy(
        update={"outcome": None}
    )

    assert "without recording an outcome" in one_line(unfinished)
    assert "did not reach the point where one is assigned" in render(unfinished)


# --- an action that never ran is not a check that failed --------------------
def test_a_dead_end_from_a_failed_action_says_so_rather_than_blaming_the_check():
    # The real case: three `Locator.fill: Timeout` failures on a login page.
    # The old wording claimed "the planner's step failed its own check", which
    # is a confident description of something that did not happen -- no check
    # ran, because the action never did.
    broken = step(
        2,
        ActionKind.TYPE,
        target_description="Roll Number textbox",
        value="23L-0842",
        intent_label="type the roll number",
        act_ok=False,
        act_error="Locator.fill: Timeout 15000ms exceeded.",
        verify_ok=None,
    )

    line = one_line(trace_of([broken], RunOutcome.DEAD_END))
    document = render(trace_of([broken], RunOutcome.DEAD_END))

    assert "could not be performed" in line
    assert "Timeout 15000ms" in line
    assert "The action itself could not be performed" in document
    assert "failed its own check" not in document


def test_a_dead_end_from_a_failed_check_still_blames_the_check():
    failed = step(0, verify_ok=False, check_performed="looked for X", evidence="saw Y")

    document = render(trace_of([failed], RunOutcome.DEAD_END))

    assert "failed its own check" in document
    assert "The action itself could not be performed" not in document
