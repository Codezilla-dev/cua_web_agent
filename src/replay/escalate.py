"""Turning a replay failure into a person's problem, and back again.

Replay is deterministic, which is exactly why it needs this: a deterministic
system that meets a condition it has no rule for has nothing left to try. The
useful move at that point is not another retry, it is a human -- and the
requirement is that they get *this* session, mid-flow, rather than a fresh
browser at the same URL.

**Not every failure escalates.** `INVALID_REQUEST` is a caller bug: no operator
can fix a missing parameter by clicking something, and waking one up to look at
it would be noise. A business outcome never escalates either -- it is an answer,
and the caller asked for it. What escalates is the class where the session is
real, the flow is live, and a person looking at the screen could plausibly
unblock it.

**The retry after resume is a single attempt.** If the human's intervention did
not fix the step, escalating again immediately would trap the run in a loop
between the two of them. One retry, then the original failure stands.
"""

import time
from typing import Any

from src.escalation.conditions import StuckCondition
from src.escalation.intervention import (
    DEFAULT_INTERVENTION_DIR,
    Intervention,
    InterventionTimeout,
    make_intervention_id,
    request_from_stuck,
)
from src.replay.outcomes import FailureKind, HardFailure
from src.session.lease import SessionControl

# Which failures a person can plausibly do something about, and what they are
# being asked to do. A failure absent from this map is one where handing over
# the session would waste an operator's time.
ESCALATABLE: dict[FailureKind, StuckCondition] = {
    FailureKind.TARGET_NOT_FOUND: StuckCondition.LOCATOR_UNRESOLVED,
    FailureKind.EXPECTATION_FAILED: StuckCondition.UNKNOWN_DIALOG,
    FailureKind.CHECKPOINT_FAILED: StuckCondition.CHECKPOINT_FAILED_TERMINAL,
    FailureKind.POLICY_HALTED: StuckCondition.POLICY_BLOCK_NEEDS_HUMAN,
    FailureKind.ACTION_FAILED: StuckCondition.LOCATOR_UNRESOLVED,
}


class EscalationPolicy:
    """How (and whether) a replay hands a stuck session to a person."""

    def __init__(
        self,
        run_id: str,
        control: SessionControl,
        redactor: Any = None,
        root: Any = DEFAULT_INTERVENTION_DIR,
        wait_timeout_s: float = 900.0,
        enabled: bool = True,
    ) -> None:
        self.run_id = run_id
        self.control = control
        self.redactor = redactor
        self.root = root
        self.wait_timeout_s = wait_timeout_s
        self.enabled = enabled

    def should_escalate(self, failure: HardFailure) -> bool:
        return self.enabled and failure.kind in ESCALATABLE

    @staticmethod
    def condition_for(failure: HardFailure) -> StuckCondition:
        """Which stuck condition this failure is, really.

        `kind` says what went wrong at the step; `stuck_hint` says what is going
        on across the run, and when the engine has one it wins. Both an expired
        session and a page that has stopped moving present at the step as an
        expectation that did not hold or a control that is not there -- and
        those send an operator to do completely different things, which is the
        whole reason this enum has more than one member.
        """
        if failure.stuck_hint:
            try:
                return StuckCondition(failure.stuck_hint)
            except ValueError:
                pass
        return ESCALATABLE[failure.kind]

    def escalate(self, failure: HardFailure, request: Any) -> Intervention | None:
        """Raise an intervention and block until an operator returns the lease.

        Returns the intervention on a successful handoff, or `None` if nobody
        came -- in which case the caller keeps the original failure, which is
        the honest outcome for a run that asked for help and did not get it.
        """
        condition = self.condition_for(failure)
        intervention = Intervention(
            make_intervention_id(self.run_id, failure.step_index), root=self.root
        )

        state = _safe_state(request, intervention)
        stuck = request_from_stuck(
            condition,
            run_id=self.run_id,
            step_index=failure.step_index,
            capability_id=request.capability.id,
            goal=f"replay {request.capability.id}",
            step_intent=failure.step_intent,
            url=state.get("url", ""),
            title=state.get("title", ""),
            screenshot_path=state.get("screenshot_path") or None,
            state_summary=failure.observed,
            detail=f"{failure.kind.value}: expected {failure.expected}",
        )

        intervention.raise_request(stuck)
        # From here the surface refuses automation: the lease says a human has
        # it, and the gate is inside the action executor.
        self.control.bind(intervention.lease)

        recorder = _start_recorder(request.surface, self.redactor)
        print(
            f"\nescalation: {stuck.condition.value} at step {failure.step_index}.\n"
            f"  cua operator take {intervention.intervention_id}\n"
            f"  waiting for an operator..."
        )
        try:
            intervention.wait_for_resume(
                timeout_s=self.wait_timeout_s, sleep=_pump_for(request.surface)
            )
        except InterventionTimeout:
            _stop_recorder(recorder, intervention)
            self.control.release()
            return None

        _stop_recorder(recorder, intervention)
        self.control.release()
        print(f"escalation: control returned; retrying step {failure.step_index}.")
        return intervention


def _pump_for(surface: Any) -> Any:
    """How to wait while a human works, without going deaf to what they do.

    A plain `time.sleep` is the obvious choice and the wrong one: the surface
    may need to be *inside* its driver for events to be delivered. Surfaces that
    have nothing to pump fall back to sleeping, which is what a headless or
    non-browser surface wants anyway.
    """
    pump = getattr(surface, "pump", None)
    if pump is None:
        return time.sleep

    def wait(seconds: float) -> None:
        try:
            pump(seconds)
        except Exception:
            time.sleep(seconds)

    return wait


def _start_recorder(surface: Any, redactor: Any) -> Any:
    """Attach a human-action recorder if this surface can produce one.

    Surfaces are not required to support recording -- the `Surface` protocol
    says nothing about it -- so a surface that cannot is a capability gap, not
    an error. The handoff still works; it is just not witnessed.
    """
    factory = getattr(surface, "human_recorder", None)
    if factory is None:
        return None
    try:
        recorder = factory(redactor)
        recorder.start()
        return recorder
    except Exception:
        return None


def _stop_recorder(recorder: Any, intervention: Intervention) -> None:
    if recorder is None:
        return
    try:
        actions = recorder.stop()
    except Exception:
        return
    if actions:
        intervention.write_human_actions(actions)


def _safe_state(request: Any, intervention: Intervention) -> dict[str, str]:
    """Where the session is, for the operator.

    A screenshot is taken alongside the structural summary rather than instead
    of it. The summary is what a person greps; the picture is what they trust
    when the summary and the screen disagree -- and disagreeing is exactly what
    a stuck run is doing.

    Never worth failing the escalation over: an operator with a URL and no
    screenshot can still work, and one with no intervention at all cannot.
    """
    intervention.directory.mkdir(parents=True, exist_ok=True)
    shot = intervention.directory / "state.png"
    try:
        state = request.surface.perception.perceive(screenshot_path=str(shot))
        return {
            "url": state.url,
            "title": state.title,
            "screenshot_path": str(shot) if shot.exists() else "",
        }
    except Exception:
        return {}
