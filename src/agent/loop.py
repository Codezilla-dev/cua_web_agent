"""The discovery loop: observe -> decide -> guard -> act -> settle -> verify.

Recovery is three attempts per logical step:

1. **retry** the same `Intent`, reusing the exact snapshot it was decided
   against -- refs are snapshot-scoped, so re-observing first would point
   `target_ref` at a different element.
2. **replan**: observe fresh and call the planner with `last_failure`.
3. **give up**: the step is a dead end and the run halts.

Each attempt is its own `StepRecord` and costs its own budget. `decide()` runs
for original and replan, never for retry -- one LLM call per step stays true.

A policy halt is final; there is no retry or replan for it.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, Protocol

from src.agent.verify import verify_expectation
from src.surface.base import (
    ACTIONS_NEEDING_TARGET,
    Surface,
    SurfaceError,
    TargetNotFoundError,
)
from src.types import (
    ActionKind,
    ActResult,
    Decision,
    Intent,
    PolicyVerdict,
    ResolvedTarget,
    RunOutcome,
    StepOutcome,
    StepRecord,
    Trace,
    UIState,
    VerifyResult,
)

logger = logging.getLogger(__name__)

# Actions whose effect shows up in `text_digest` (built from innerText).
# TYPE and SELECT change an element's *value*, which innerText never
# includes -- listing them here flagged every successful type as a no-op.
DIGEST_VISIBLE_ACTIONS = {
    ActionKind.NAVIGATE,
    ActionKind.CLICK,
}

# "retry" reuses the original Intent; the other two observe and decide fresh.
RecoveryStage = Literal["original", "retry", "replan"]
RECOVERY_STAGES: tuple[RecoveryStage, ...] = ("original", "retry", "replan")

_STAGE_SUCCESS_OUTCOME: dict[RecoveryStage, StepOutcome] = {
    "original": StepOutcome.OK,
    "retry": StepOutcome.RETRIED,
    "replan": StepOutcome.REPLANNED,
}


class Planner(Protocol):
    """What the loop needs from a planner. A stub satisfies it too."""

    def decide(
        self,
        goal: str,
        state: UIState,
        history: list[str],
        last_failure: str | None,
    ) -> Decision: ...


class PolicyChecker(Protocol):
    """What the loop needs from the guard stage."""

    def check(self, intent: Intent, state: UIState, target_url: str) -> PolicyVerdict: ...


class EvidenceSink(Protocol):
    """What the loop needs from the evidence writer."""

    run_id: str

    def step_png_path(self, index: int) -> str: ...

    def append_step(self, step: StepRecord) -> StepRecord:
        """Persist `step`, and return the redacted copy that was written.

        The loop renders the planner's history from what comes back, so an
        implementation that scrubs on the way to disk scrubs the prompt too.
        """
        ...

    def write_run(self, trace: Trace) -> None: ...


@dataclass(frozen=True)
class Budget:
    """Stopping conditions, independent of the planner declaring DONE."""

    max_steps: int
    max_seconds: float


@dataclass
class _LoopState:
    """Bookkeeping threaded through one run, mutated as steps complete."""

    index: int = 0
    history: list[str] = field(default_factory=list)
    last_failure: str | None = None
    # What no-op detection compares the next fresh observation against.
    previous_digest: str | None = None
    previous_action_was_digest_visible: bool = False


@dataclass
class _Attempt:
    """One attempt's result, plus what the caller needs to decide next."""

    step_record: StepRecord
    succeeded: bool
    halted: bool
    is_done: bool
    state_before: UIState
    intent: Intent
    decision: Decision
    failure_detail: str | None


def run_discovery(
    goal: str,
    target_url: str,
    surface: Surface,
    planner: Planner,
    policy: PolicyChecker,
    evidence: EvidenceSink,
    budget: Budget,
) -> Trace:
    """Drive `surface` toward `goal`, recording every step as evidence.

    Always returns a `Trace`, even on an unhandled exception -- it is caught
    and recorded as `RunOutcome.ERROR` rather than re-raised.
    """
    trace = Trace(
        run_id=evidence.run_id,
        goal=goal,
        target_url=target_url,
        started_at=datetime.now(UTC),
    )
    run_started_perf = time.monotonic()
    state = _LoopState()

    try:
        surface.start()
        surface.goto(target_url)

        while True:
            if _budget_exhausted(state.index, budget, run_started_perf):
                trace.outcome = RunOutcome.BUDGET_EXCEEDED
                break

            outcome = _run_logical_step(
                state=state,
                surface=surface,
                planner=planner,
                policy=policy,
                evidence=evidence,
                trace=trace,
                goal=goal,
                target_url=target_url,
                budget=budget,
                run_started_perf=run_started_perf,
            )
            if outcome is None:
                continue  # this step succeeded; move on to the next one
            trace.outcome = outcome
            break

    except Exception as error:
        # Swallowed into the trace so earlier steps' evidence survives.
        logger.exception("discovery run %s ended with an unhandled exception", trace.run_id)
        trace.outcome = RunOutcome.ERROR
        trace.error = str(error)
    finally:
        trace.finished_at = datetime.now(UTC)
        evidence.write_run(trace)
        surface.stop()

    return trace


def _run_logical_step(
    *,
    state: _LoopState,
    surface: Surface,
    planner: Planner,
    policy: PolicyChecker,
    evidence: EvidenceSink,
    trace: Trace,
    goal: str,
    target_url: str,
    budget: Budget,
    run_started_perf: float,
) -> RunOutcome | None:
    """Resolve one planner-visible step, retrying and re-planning on failure.

    Returns the `RunOutcome` that should end the whole run (the guard halted,
    DONE verified, recovery ran out, or the budget ran out mid-recovery), or
    `None` if the step succeeded and the run should continue.
    """
    # Set once "original" fails, so "retry" can reuse that exact attempt.
    reuse_state_before: UIState | None = None
    reuse_intent: Intent | None = None
    reuse_decision: Decision | None = None

    for stage in RECOVERY_STAGES:
        if _budget_exhausted(state.index, budget, run_started_perf):
            return RunOutcome.BUDGET_EXCEEDED

        if stage == "retry":
            # Re-observing would reassign refs and move target_ref.
            assert reuse_state_before is not None
            state_before = reuse_state_before
        else:
            state_before = _observe(surface, evidence, state.index)
            no_op_note = _check_no_op(
                state_before, state.previous_digest, state.previous_action_was_digest_visible
            )
            if no_op_note is not None:
                state.last_failure = no_op_note
            state.previous_digest = state_before.text_digest

        attempt = _run_one_attempt(
            index=state.index,
            stage=stage,
            surface=surface,
            planner=planner,
            policy=policy,
            goal=goal,
            target_url=target_url,
            history=state.history,
            last_failure=state.last_failure,
            state_before=state_before,
            intent=reuse_intent if stage == "retry" else None,
            decision=reuse_decision if stage == "retry" else None,
            recovery_note=_recovery_note(stage, state.last_failure),
        )

        redacted_record = evidence.append_step(attempt.step_record)
        trace.steps.append(attempt.step_record)
        state.history.append(_history_line(redacted_record))
        state.index += 1
        state.previous_action_was_digest_visible = attempt.intent.action in DIGEST_VISIBLE_ACTIONS

        if attempt.halted:
            return RunOutcome.HALTED_POLICY
        if attempt.is_done:
            return RunOutcome.COMPLETED
        if attempt.succeeded:
            state.last_failure = None
            return None

        # Carry the failure forward for the next stage.
        state.last_failure = attempt.failure_detail
        reuse_state_before = attempt.state_before
        reuse_intent = attempt.intent
        reuse_decision = attempt.decision

    # original, retry, and replan all failed verification.
    return RunOutcome.DEAD_END


def _run_one_attempt(
    *,
    index: int,
    stage: RecoveryStage,
    surface: Surface,
    planner: Planner,
    policy: PolicyChecker,
    goal: str,
    target_url: str,
    history: list[str],
    last_failure: str | None,
    state_before: UIState,
    intent: Intent | None,
    decision: Decision | None,
    recovery_note: str | None,
) -> _Attempt:
    """Run decide (unless `intent` is supplied, for a retry) through verify.

    `intent`/`decision` are given together or not at all.
    """
    started_at = datetime.now(UTC)
    started_perf = time.monotonic()

    if intent is None:
        decision = planner.decide(goal, state_before, history, last_failure)
        intent = decision.intent
    assert decision is not None  # guaranteed by one of the two branches above

    policy_verdict = policy.check(intent, state_before, target_url)

    if policy_verdict.disposition == "halt":
        record = StepRecord(
            index=index,
            started_at=started_at,
            duration_ms=_elapsed_ms(started_perf),
            state_before=state_before,
            decision=decision,
            policy=policy_verdict,
            act=None,
            verify=None,
            outcome=StepOutcome.HALTED,
            recovery_note=recovery_note,
            screenshot_path=state_before.screenshot_path,
        )
        return _Attempt(
            step_record=record,
            succeeded=False,
            halted=True,
            is_done=False,
            state_before=state_before,
            intent=intent,
            decision=decision,
            failure_detail=policy_verdict.reason,
        )

    act_result, failure_detail = _act(surface, intent, state_before)

    verify_result: VerifyResult | None = None
    is_done = False
    if act_result is not None and act_result.ok:
        fresh_state = surface.perception.perceive()
        verify_result = verify_expectation(intent.expectation, fresh_state)
        if verify_result.ok:
            is_done = intent.action is ActionKind.DONE
        else:
            failure_detail = f"expectation not met: {verify_result.evidence}"
    elif act_result is not None:  # act_result.ok is False
        failure_detail = f"action failed: {act_result.error}"
    # else: act_result is None; failure_detail was already set by _act().

    succeeded = verify_result is not None and verify_result.ok
    outcome = _STAGE_SUCCESS_OUTCOME[stage] if succeeded else StepOutcome.VERIFY_FAILED

    record = StepRecord(
        index=index,
        started_at=started_at,
        duration_ms=_elapsed_ms(started_perf),
        state_before=state_before,
        decision=decision,
        policy=policy_verdict,
        act=act_result,
        verify=verify_result,
        outcome=outcome,
        recovery_note=recovery_note,
        screenshot_path=state_before.screenshot_path,
    )
    return _Attempt(
        step_record=record,
        succeeded=succeeded,
        halted=False,
        is_done=is_done,
        state_before=state_before,
        intent=intent,
        decision=decision,
        failure_detail=failure_detail,
    )


def _act(
    surface: Surface, intent: Intent, state_before: UIState
) -> tuple[ActResult | None, str | None]:
    """Resolve a target if needed, execute, and attach settle.

    `(None, detail)` when the action could not be attempted at all; otherwise
    `(ActResult, None)`, even for a failed one -- the caller reads its error.
    """
    target: ResolvedTarget | None = None
    if intent.action in ACTIONS_NEEDING_TARGET:
        if intent.target_ref is None:
            return None, f"{intent.action} requires a target_ref but none was given"
        try:
            target = surface.perception.resolve(intent.target_ref, state_before)
        except TargetNotFoundError as error:
            return None, f"target not found: {error}"

    try:
        act_result = surface.actions.execute(intent, target)
    except SurfaceError as error:
        return None, f"action could not run: {error}"

    settle_result = surface.settle.wait_until_settled(timeout_seconds=surface.settle_timeout_s)
    act_result = act_result.model_copy(update={"settle": settle_result})
    return act_result, None


def _observe(surface: Surface, evidence: EvidenceSink, index: int) -> UIState:
    """Perceive the current UI, capturing a screenshot as evidence."""
    screenshot_path = evidence.step_png_path(index)
    state = surface.perception.perceive(screenshot_path)
    if not state.coverage.ok:
        logger.warning("step %d: perception coverage degraded -- %s", index, state.coverage.reason)
    return state


def _check_no_op(
    state: UIState,
    previous_digest: str | None,
    previous_action_was_digest_visible: bool,
) -> str | None:
    """A note if the last navigate/click left `text_digest` unchanged."""
    if previous_digest is None or not previous_action_was_digest_visible:
        return None
    if state.text_digest != previous_digest:
        return None
    return (
        "the previous action did not visibly change the page "
        "(text digest unchanged after a navigate or click)"
    )


def _recovery_note(stage: RecoveryStage, previous_failure: str | None) -> str | None:
    if stage == "retry":
        return f"retry after: {previous_failure}"
    if stage == "replan":
        return f"replan after: {previous_failure}"
    return None


def _history_line(step: StepRecord) -> str:
    """One line per step for the planner's history. Never a `UIState`.

    A `read` reports what it returned, and what it was reading. Without that
    the planner sees `read on e42 -> ok` and has no way to know the value ever
    arrived, so it reads again -- which is exactly what happened on ParaBank,
    three times against the same total. Refs are renumbered each observation,
    so the ref alone does not identify the row either.

    Pass the *redacted* record: history persists for the rest of the run, and
    a value the redaction pattern covers should not.
    """
    intent = step.decision.intent
    target = f" on {intent.target_ref}" if intent.target_ref else ""
    value = f" = {intent.value!r}" if intent.value else ""
    note = f" [{step.recovery_note}]" if step.recovery_note else ""
    result = _read_result(step)
    return (
        f"step {step.index}: {intent.action.value}{target}{value} "
        f"-> {step.outcome.value}{result}{note}"
    )


def _read_result(step: StepRecord) -> str:
    """What a `read` came back with, for the planner's history.

    An empty read is reported rather than left blank. "The element was there
    and held nothing" and "no read happened" are different facts, and only one
    of them is a reason to try again.
    """
    if step.decision.intent.action is not ActionKind.READ:
        return ""
    described = step.decision.intent.target_description
    subject = f" {described}" if described else ""
    if step.act is None or not step.act.read_value:
        return f" (read nothing from{subject or ' the element'})"
    return f" (read{subject} = {step.act.read_value!r})"


def _budget_exhausted(index: int, budget: Budget, run_started_perf: float) -> bool:
    elapsed_seconds = time.monotonic() - run_started_perf
    return index >= budget.max_steps or elapsed_seconds >= budget.max_seconds


def _elapsed_ms(started_perf: float) -> int:
    return int((time.monotonic() - started_perf) * 1000)
