"""The planner has to be able to see what its own `read` steps returned.

Found by running against ParaBank with the goal "Find the total money in all
accounts". The run succeeded, but it read the same figure three times:

    3. read 'total balance amount' -- read `$515.50`
    4. read 'total account balance' -- read `$515.50`
    5. read 'total account balance' -- read `$515.50`

The planner was not being careless. `_history_line` rendered every step as
`step 3: read on e42 -> ok` -- action, target ref, outcome. The value the read
produced was never in it, and `intent.value` is what the planner asked to
*type*, not what came back. So from the planner's side the read had no visible
result, and reading again was the reasonable move. Element refs are renumbered
on each observation too, so `e42` does not even identify the row it already
looked at.

It compiles into a bad capability: `src/artifact/compiler.py` turns every
`read` into an `OutputSpec`, so one number arrived as two outputs named
`total_balance_amount` and `total_account_balance`. A caller reading that
schema cannot tell they are the same figure.

The history line is built from the *redacted* record for the same reason the
trace is: a read of a field the redaction pattern covers should not sit in the
prompt for the rest of the run. The model saw that text once, in the page
snapshot it was deciding against; history is what makes an exposure persist.
"""

from datetime import UTC, datetime

from src.agent.loop import _history_line
from src.evidence import EvidenceWriter
from src.policy.redact import Redactor
from src.types import (
    ActionKind,
    ActResult,
    Decision,
    Expectation,
    Intent,
    PerceptionCoverage,
    PolicyVerdict,
    RiskTier,
    StepOutcome,
    StepRecord,
    TextVisibleCheck,
    UIState,
)


def _step(
    action: ActionKind,
    *,
    target_ref: str | None = None,
    target_description: str = "",
    value: str | None = None,
    read_value: str | None = None,
) -> StepRecord:
    return StepRecord(
        index=3,
        started_at=datetime.now(UTC),
        duration_ms=1,
        state_before=UIState(
            url="https://example.test/",
            title="t",
            elements=[],
            text_digest="digest",
            coverage=PerceptionCoverage(
                ok=True, reason=None, interactive_count=0, unnamed_ratio=0.0
            ),
            captured_at=datetime.now(UTC),
        ),
        decision=Decision(
            intent=Intent(
                reasoning="because",
                intent="do the thing",
                action=action,
                target_ref=target_ref,
                target_description=target_description,
                value=value,
                risk=RiskTier.SAFE,
                expectation=Expectation(
                    description="something is visible",
                    check=TextVisibleCheck(kind="text_visible", text="something"),
                ),
            ),
            model="test",
            latency_ms=1,
        ),
        policy=PolicyVerdict(
            allowed=True, tier=RiskTier.SAFE, rule="test", reason="ok", disposition="allow"
        ),
        act=ActResult(ok=True, action=action, duration_ms=1, read_value=read_value),
        outcome=StepOutcome.OK,
    )


def test_a_read_step_reports_the_value_it_returned():
    line = _history_line(
        _step(
            ActionKind.READ,
            target_ref="e42",
            target_description="total balance amount",
            read_value="$515.50",
        )
    )

    assert "$515.50" in line, (
        f"the planner cannot see what its own read returned: {line!r} -- which is why "
        "the ParaBank run read the same total three times and compiled two outputs "
        "for one number"
    )


def test_a_read_step_names_what_it_read_not_just_the_ref():
    # Refs are renumbered on every observation, so `e42` does not tell the
    # planner which row this was. The description does.
    line = _history_line(
        _step(
            ActionKind.READ,
            target_ref="e42",
            target_description="total balance amount",
            read_value="$515.50",
        )
    )

    assert "total balance amount" in line


def test_a_non_read_step_is_unchanged():
    line = _history_line(
        _step(ActionKind.CLICK, target_ref="e7", target_description="Log In button")
    )

    assert line == "step 3: click on e7 -> ok"


def test_a_read_that_returned_nothing_says_so_rather_than_going_quiet():
    # A read that came back empty is information: it means the element was
    # there and held no text. Rendering it identically to a read that was
    # never attempted is what sends the planner round again.
    line = _history_line(
        _step(ActionKind.READ, target_ref="e42", target_description="balance", read_value=None)
    )

    assert "read nothing" in line


def test_the_history_line_is_built_from_the_redacted_record(tmp_path):
    # Not a new exposure in itself -- the model saw the page text when it chose
    # to read it -- but history persists for the rest of the run, and the
    # redaction pattern exists to say which values should not.
    redactor = Redactor(
        sensitive_name_pattern=r"(?i)password|token|api key",
        replacement="***REDACTED***",
        secret_values=(),
    )
    step = _step(
        ActionKind.READ,
        target_ref="e9",
        target_description="API key field",
        read_value="sk-live-abcd1234",
    )

    line = _history_line(redactor.redact_step(step))

    assert "sk-live-abcd1234" not in line


def test_the_writer_hands_back_the_record_it_redacted(tmp_path):
    # The loop uses this return value for history, so the writer redacting one
    # copy and the loop rendering another would put the raw value back.
    writer = EvidenceWriter(
        run_id="testrun",
        root=tmp_path,
        redactor=Redactor(
            sensitive_name_pattern=r"(?i)password|token|api key",
            replacement="***REDACTED***",
            secret_values=(),
        ),
    )
    step = _step(
        ActionKind.READ,
        target_ref="e9",
        target_description="API key field",
        read_value="sk-live-abcd1234",
    )

    returned = writer.append_step(step)

    assert returned is not None
    assert "sk-live-abcd1234" not in _history_line(returned)
