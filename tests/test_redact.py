"""Tests for `Redactor.redact_step`.

Scrub on a sensitive element name, scrub on a sensitive target description,
and an unchanged step returned as the *same object*.
"""

from src.policy.redact import Redactor
from src.types import (
    ActionKind,
    Decision,
    Expectation,
    FieldHasValueCheck,
    Intent,
    PolicyVerdict,
    RiskTier,
    StepOutcome,
    StepRecord,
    TextVisibleCheck,
    VerifyResult,
)
from tests.conftest import make_element, make_state

REPLACEMENT = "***REDACTED***"


def make_redactor(pattern=r"(?i)password|passcode|pin") -> Redactor:
    return Redactor(sensitive_name_pattern=pattern, replacement=REPLACEMENT)


def make_step(target_ref, target_description="", value=None, elements=()) -> StepRecord:
    state = make_state(elements=list(elements))
    intent = Intent(
        reasoning="test",
        intent="test step",
        action=ActionKind.TYPE,
        target_ref=target_ref,
        target_description=target_description,
        value=value,
        risk=RiskTier.SAFE,
        expectation=Expectation(description="n/a", check=TextVisibleCheck(text="n/a")),
    )
    decision = Decision(intent=intent, model="test-model", latency_ms=0)
    policy_verdict = PolicyVerdict(
        allowed=True, tier=RiskTier.SAFE, rule="allow", reason="test", disposition="allow"
    )
    return StepRecord(
        index=0,
        started_at=state.captured_at,
        duration_ms=0,
        state_before=state,
        decision=decision,
        policy=policy_verdict,
        outcome=StepOutcome.OK,
    )


def test_redact_step_scrubs_value_when_the_target_elements_name_is_sensitive():
    step = make_step(
        target_ref="e1",
        value="hunter2",
        elements=[make_element("e1", role="textbox", name="Passcode")],
    )

    redacted = make_redactor().redact_step(step)

    assert redacted.decision.intent.value == REPLACEMENT
    # The original, in-memory step is untouched -- only the copy handed to the
    # evidence writer is scrubbed; `trace.steps` must keep the real value.
    assert step.decision.intent.value == "hunter2"


def test_redact_step_scrubs_value_when_target_description_is_sensitive():
    step = make_step(
        target_ref=None,
        target_description="the account PIN field",
        value="1234",
        elements=[],
    )

    redacted = make_redactor().redact_step(step)

    assert redacted.decision.intent.value == REPLACEMENT


def test_redact_step_leaves_a_non_sensitive_step_unchanged():
    step = make_step(
        target_ref="e1",
        value="12345",
        elements=[make_element("e1", role="textbox", name="Member ID")],
    )

    redacted = make_redactor().redact_step(step)

    assert redacted.decision.intent.value == "12345"
    # No match -> no copy: the exact same object comes back.
    assert redacted is step


# --------------------------------------------------------------------------- #
# Value sweep
#
# These pin the leak found in committed evidence: scrubbing `intent.value` alone
# left the password readable in seven other places. Each assertion below is one
# of those places, taken from a real `trace.jsonl`.
# --------------------------------------------------------------------------- #
SECRET = "hunter2"


def make_leaky_step() -> StepRecord:
    """A step with the password in every place the real trace leaked it."""
    state = make_state(
        elements=[
            # The label survives; the typed value read back off the DOM does not.
            make_element("e5", role="StaticText", name="Operator ID", value="Operator ID"),
            make_element("e6", role="textbox", name="Operator ID", value=SECRET),
            make_element("e7", role="StaticText", name=SECRET, value=SECRET),
            make_element("e9", role="textbox", name="Passcode"),
        ]
    )
    intent = Intent(
        reasoning=f"The username has been entered. Now enter the password '{SECRET}'.",
        intent=f"Enter the password '{SECRET}' for the operator account",
        action=ActionKind.TYPE,
        target_ref="e9",
        target_description="Passcode textbox",
        value=SECRET,
        risk=RiskTier.SAFE,
        expectation=Expectation(
            description=f"The Passcode field should now contain '{SECRET}'",
            check=FieldHasValueCheck(name_contains="Passcode", expected=SECRET),
        ),
    )
    verify = VerifyResult(
        ok=False,
        expectation=intent.expectation,
        check_performed=f"expected its value to be '{SECRET}' (case-insensitive)",
        evidence=f"matched fields held [\"e9='Passcode'\", \"e10='{SECRET}'\"]",
    )
    return StepRecord(
        index=1,
        started_at=state.captured_at,
        duration_ms=0,
        state_before=state,
        decision=Decision(intent=intent, model="test-model", latency_ms=0),
        policy=PolicyVerdict(
            allowed=True, tier=RiskTier.SAFE, rule="allow", reason="t", disposition="allow"
        ),
        verify=verify,
        outcome=StepOutcome.VERIFY_FAILED,
    )


def test_value_sweep_removes_the_secret_from_every_place_it_leaked():
    redactor = Redactor(
        sensitive_name_pattern=r"(?i)passcode", replacement=REPLACEMENT, secret_values=[SECRET]
    )

    redacted = redactor.redact_step(make_leaky_step())

    assert SECRET not in redacted.model_dump_json()


def test_value_sweep_covers_each_leak_path_individually():
    redactor = Redactor(
        sensitive_name_pattern=r"(?i)passcode", replacement=REPLACEMENT, secret_values=[SECRET]
    )

    step = redactor.redact_step(make_leaky_step())
    intent = step.decision.intent
    assert step.verify is not None

    assert SECRET not in intent.reasoning
    assert SECRET not in intent.intent
    assert SECRET not in intent.expectation.description
    assert isinstance(intent.expectation.check, FieldHasValueCheck)
    assert intent.expectation.check.expected == REPLACEMENT
    assert SECRET not in step.verify.check_performed
    assert SECRET not in step.verify.evidence
    assert all(SECRET not in (element.value or "") for element in step.state_before.elements)
    assert all(SECRET not in element.name for element in step.state_before.elements)


def test_value_sweep_is_literal_and_spares_lookalike_labels():
    # Password "operator" must not blank out the "Operator ID" label: we scrub
    # the secret as typed, not every word that resembles it.
    redactor = Redactor(
        sensitive_name_pattern=r"(?i)passcode",
        replacement=REPLACEMENT,
        secret_values=["operator"],
    )
    state = make_state(
        elements=[
            make_element("e5", role="StaticText", name="Operator ID", value="Operator ID"),
            make_element("e6", role="textbox", name="Operator ID", value="operator"),
        ]
    )
    step = make_leaky_step().model_copy(update={"state_before": state})

    redacted = redactor.redact_step(step)

    assert redacted.state_before.elements[0].value == "Operator ID"
    assert redacted.state_before.elements[1].value == REPLACEMENT


def test_value_sweep_prefers_the_longest_secret_when_one_contains_another():
    # Scrubbing "pass" first would leave "***REDACTED***word" behind.
    redactor = Redactor(
        sensitive_name_pattern=r"(?i)nothing",
        replacement=REPLACEMENT,
        secret_values=["pass", "password123"],
    )

    assert redactor._scrub_text("password123") == REPLACEMENT


def test_no_secrets_configured_leaves_a_clean_step_identical():
    step = make_step(
        target_ref="e1",
        value="12345",
        elements=[make_element("e1", role="textbox", name="Member ID")],
    )

    assert Redactor(r"(?i)passcode", REPLACEMENT, secret_values=[]).redact_step(step) is step
