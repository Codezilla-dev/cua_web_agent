"""What happened, in the words a person would use.

Every run already writes `trace.jsonl` -- one JSON object per step, carrying the
state before it, the planner's reasoning, the policy verdict, the action result
and the verification result. That file is the evidence, and it is the right
shape for an auditor with a question. It is the wrong shape for the much more
common question, which is "so what happened?"

So this renders the same trace as prose. Two outputs, from the same data:

  `one_line`  the single sentence that goes on `Trace.summary`, and so into
              `run.json` -- enough to know the shape of a run without opening
              anything else.
  `render`    the human-readable `summary.md` in the evidence bundle.

**Deterministic, not model-written.** Every word here is derived from the trace
by rule. A model could write nicer prose, but a summary in an evidence bundle
has to be something you can check against the trace it claims to describe, and
one that was generated could be wrong in ways the trace is not -- the same
argument that stopped replay from reusing the discovery `StepRecord` and
inventing a `Decision` no model produced. It also costs no API call, which
matters because this is written for *every* run including the ones that failed
before any useful work happened.

**Written whatever the outcome.** The runs worth explaining to a person are
mostly the ones that did not reach their goal: a dead end, a policy halt, an
exhausted budget, an unhandled exception. `run_discovery` writes this from its
`finally` block for that reason, so a crash still leaves an account of itself.

**Credentials are not printed, and are scrubbed anyway.** A typed value goes in
only when the field it went into is not a credential field, decided by the same
`is_unobservable_field` rule the redactor and the compiler use. The writer
sweeps the rendered text as a backstop, because the sweep is the mechanism this
project actually relies on.
"""

from datetime import timedelta

from src.agent.verify import is_unobservable_field
from src.types import ActionKind, RunOutcome, StepOutcome, StepRecord, Trace

# How a run's outcome reads at the start of a sentence.
_OUTCOME_PHRASE: dict[RunOutcome, str] = {
    RunOutcome.COMPLETED: "Completed",
    RunOutcome.DEAD_END: "Dead end",
    RunOutcome.HALTED_POLICY: "Halted by policy",
    RunOutcome.BUDGET_EXCEEDED: "Budget exceeded",
    RunOutcome.ERROR: "Ended with an error",
}

_CREDENTIAL_PLACEHOLDER = "(not recorded)"


# --------------------------------------------------------------------------- #
# The one-liner
# --------------------------------------------------------------------------- #
def one_line(trace: Trace) -> str:
    """A single sentence describing the run, for `Trace.summary`."""
    outcome = trace.outcome
    steps = len(trace.steps)
    # `outcome` is optional on `Trace` and is assigned as the run ends, so a
    # trace can genuinely reach here without one -- a crash between the last
    # step and the assignment. Saying so is better than a summary that implies
    # the run finished tidily.
    if outcome is None:
        return f"Ended after {_plural(steps, 'step')} without recording an outcome."
    head = f"{_OUTCOME_PHRASE[outcome]} after {_plural(steps, 'step')}"

    if outcome is RunOutcome.COMPLETED:
        reads = _read_values(trace)
        if not reads:
            return f"{head}, reading no values."
        parts = ", ".join(f"{name} = {value!r}" for name, value in reads)
        return f"{head}, reading {parts}."

    if outcome is RunOutcome.ERROR:
        return f"{head}: {trace.error or 'no reason was recorded'}."

    if outcome is RunOutcome.HALTED_POLICY:
        halted = _last_step_with(trace, StepOutcome.HALTED)
        if halted is not None:
            return (
                f"{head}: step {halted.index} ({halted.decision.intent.intent!r}) was stopped "
                f"by {halted.policy.rule} -- {halted.policy.reason}."
            )
        return f"{head}: policy stopped the run."

    if outcome is RunOutcome.BUDGET_EXCEEDED:
        return f"{head}: the step or time budget ran out before the goal was reached."

    # DEAD_END, and anything a later version adds.
    failed = _last_failed_step(trace)
    if failed is None:
        return f"{head}, with nothing left to try."
    label = failed.decision.intent.intent
    if failed.act is not None and not failed.act.ok:
        return (
            f"{head}: {label!r} could not be performed at all, even after a retry and a "
            f"replan -- {failed.act.error}"
        )
    return (
        f"{head}: {label!r} failed its check even after a retry and a "
        f"replan ({_expected_observed(failed)})."
    )


# --------------------------------------------------------------------------- #
# The readable file
# --------------------------------------------------------------------------- #
def render(trace: Trace) -> str:
    """The whole run as `summary.md`."""
    lines = [
        f"# {trace.goal}",
        "",
        one_line(trace),
        "",
        f"- run: `{trace.run_id}`",
        f"- target: {trace.target_url}",
        f"- took: {_duration(trace)}",
        "",
        "## What it did",
        "",
    ]
    lines.append("Numbered as in `trace.jsonl`, so a line here and a line there "
                 "are the same step.")
    lines.append("")
    lines.extend(_step_lines(trace) or ["Nothing -- the run ended before any step ran."])

    lines += ["", "## What it produced", ""]
    reads = _read_values(trace)
    if reads:
        lines.extend(f"- **{name}** = `{value}`" for name, value in reads)
    else:
        lines.append("Nothing was read. No value was extracted from the page.")

    recovered = [step for step in trace.steps if step.recovery_note]
    if recovered:
        lines += ["", "## Where it had to recover", ""]
        lines.extend(f"- step {step.index}: {step.recovery_note}" for step in recovered)

    lines += ["", "## Why it stopped", "", _why_it_stopped(trace)]
    return "\n".join(lines) + "\n"


def _step_lines(trace: Trace) -> list[str]:
    """One bullet per step, plus the failure detail where there is one."""
    lines: list[str] = []
    for step in trace.steps:
        lines.append(f"{step.index}. {_describe(step)} -- {_verdict(step)}")
        verify = step.verify
        if verify is not None and not verify.ok:
            lines.append(f"   - expected: {verify.check_performed}")
            lines.append(f"   - observed: {verify.evidence}")
        elif step.act is not None and not step.act.ok:
            lines.append(f"   - the action did not run: {step.act.error}")
        elif step.policy.disposition == "halt":
            lines.append(f"   - {step.policy.rule}: {step.policy.reason}")
    return lines


def _describe(step: StepRecord) -> str:
    """What the step tried to do, without naming a secret."""
    intent = step.decision.intent
    action = intent.action
    target = intent.target_description or "the page"

    if action is ActionKind.DONE:
        return "declared the goal reached"
    if action is ActionKind.NAVIGATE:
        return f"navigated to {intent.value or 'the target'}"
    if action is ActionKind.WAIT:
        return "waited for the page to settle"
    if action is ActionKind.CLICK:
        return f"clicked {target!r}"
    if action is ActionKind.READ:
        return f"read {target!r}"
    if action is ActionKind.TYPE:
        return f"typed {_typed_value(step)} into {target!r}"
    if action is ActionKind.SELECT:
        return f"selected {_typed_value(step)} in {target!r}"
    return f"{action.value} on {target!r}"


def _typed_value(step: StepRecord) -> str:
    """The value as it may be shown -- never a credential.

    Decided by the field it went into, which is the same rule the redactor and
    the compiler use, so all three agree about what counts as a secret.
    """
    intent = step.decision.intent
    if intent.value is None:
        return "nothing"
    if is_unobservable_field(intent.target_description):
        return _CREDENTIAL_PLACEHOLDER
    return repr(intent.value)


def _verdict(step: StepRecord) -> str:
    """How the step ended, in words rather than an enum name."""
    if step.policy.disposition == "halt":
        return "**stopped by policy**"
    if step.act is not None and not step.act.ok:
        return "**the action failed**"
    verify = step.verify
    if verify is not None and not verify.ok:
        suffix = {
            StepOutcome.RETRIED: " (a retry)",
            StepOutcome.REPLANNED: " (after replanning)",
        }.get(step.outcome, "")
        return f"**its check did not hold**{suffix}"
    if step.act is not None and step.act.read_value:
        return f"read `{step.act.read_value}`"
    return "ok"


def _why_it_stopped(trace: Trace) -> str:
    outcome = trace.outcome
    if outcome is None:
        return (
            "The run stopped without recording an outcome, which means it did not "
            "reach the point where one is assigned. The steps above are what it "
            "managed before that."
        )
    if outcome is RunOutcome.COMPLETED:
        return (
            "The planner declared the goal reached and the check it named held. "
            "A capability was compiled from this run."
        )
    if outcome is RunOutcome.ERROR:
        return (
            f"Something went wrong that the loop could not handle: {trace.error}\n\n"
            "The steps above still ran and their evidence is intact -- the run is "
            "recorded up to the point it broke."
        )
    if outcome is RunOutcome.HALTED_POLICY:
        return (
            "The safety policy classified an action as one automation must not take "
            "on its own, and stopped before it ran. This is the system working, not "
            "failing: no capability is compiled from a halted run."
        )
    if outcome is RunOutcome.BUDGET_EXCEEDED:
        return (
            "The run used its whole step or time budget without reaching the goal. "
            "Either the goal needs more room (`--max-steps`, `--max-seconds`) or the "
            "flow is not converging."
        )
    tail = (
        " With all three exhausted there was nothing left to try, so the run stopped "
        "rather than continuing blindly. No capability is compiled from a run that did "
        "not reach its goal -- it would encode the failure."
    )
    failed = _last_failed_step(trace)
    if failed is not None and failed.act is not None and not failed.act.ok:
        return (
            "The action itself could not be performed -- the planner chose a control and "
            "the browser could not act on it, on the original attempt, the retry, and "
            "after replanning. That is a different problem from a step that ran and "
            "produced the wrong result: nothing happened at all." + tail
        )
    return (
        "The planner's step failed its own check, the retry failed it, and the replan "
        "failed it too." + tail
    )


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _read_values(trace: Trace) -> list[tuple[str, str]]:
    """Every value a `read` step actually returned, oldest first."""
    values: list[tuple[str, str]] = []
    for step in trace.steps:
        intent = step.decision.intent
        if intent.action is not ActionKind.READ or step.act is None or not step.act.read_value:
            continue
        name = intent.target_description or intent.intent or f"step {step.index}"
        values.append((name, step.act.read_value))
    return values


def _last_failed_step(trace: Trace) -> StepRecord | None:
    """The last step that did not work, whether the action or the check failed.

    Both matter and they are different stories: "the control was found and the
    click did nothing useful" is not "the control could not be clicked at all".
    """
    for step in reversed(trace.steps):
        if step.act is not None and not step.act.ok:
            return step
        if step.verify is not None and not step.verify.ok:
            return step
    return None


def _last_step_with(trace: Trace, outcome: StepOutcome) -> StepRecord | None:
    for step in reversed(trace.steps):
        if step.outcome is outcome:
            return step
    return None


def _expected_observed(step: StepRecord) -> str:
    verify = step.verify
    if verify is None:
        return "no check was recorded"
    return f"expected {verify.check_performed}; observed {verify.evidence}"


def _duration(trace: Trace) -> str:
    if trace.finished_at is None:
        return "unknown (the run did not finish)"
    return _humanise(trace.finished_at - trace.started_at)


def _humanise(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m {seconds % 60:02d}s"


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


__all__ = ["one_line", "render"]
