"""Raising a stuck run to a person, and waiting for them to hand it back.

The shape of the handoff, and why it is this shape:

    run hits a stuck condition
      -> writes interventions/<id>/request.json      (context a human can act on)
      -> sets the lease to HUMAN-pending             (automation stops acting)
      -> blocks in wait_for_resume()

    operator take <id>                               (claims the HUMAN lease)
      -> human acts in the same headed browser
      -> recorder captures what they did

    operator resume <id>                             (returns the lease)
      -> run writes human-actions.jsonl into the bundle
      -> run continues from the next step

The browser is never restarted, re-launched, or re-navigated across that
boundary. That is the whole requirement: the human gets the session as it
actually is, mid-flow, cookies and scroll position and half-filled form
included. A handoff that hands over a fresh browser at the same URL is a
different and much easier problem, and it is not the one that occurs in
production.

**Why files and polling rather than a socket.** The two parties are separate
processes with no shared parent, and the operator may take minutes to arrive. A
directory both can see needs no daemon, survives either side restarting, and
leaves the intervention readable afterwards as evidence. The cost is a poll
loop, which is a real cost and is bounded here rather than hidden: a fixed
interval and a timeout that fails loudly instead of blocking forever.
"""

import json
import time
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from src.escalation.conditions import OPERATOR_GUIDANCE, StuckCondition
from src.session.lease import UNCLAIMED, ControlLease, LeaseOwner, LeaseStore

DEFAULT_INTERVENTION_DIR = Path("interventions")

# How often to look for the operator, and how long to wait before giving up.
# Polling is cheap next to a human's reaction time; the timeout exists so an
# unattended run fails with a reason rather than hanging until someone notices.
POLL_INTERVAL_S = 1.0
DEFAULT_WAIT_TIMEOUT_S = 900.0

# What the run should do with the step it was stuck on, once control returns.
RETRY_STEP = "retry"
STEP_ALREADY_PERFORMED = "performed"


class InterventionRequest(BaseModel):
    """Everything a person needs to pick up a stuck run without reading code."""

    model_config = ConfigDict(extra="forbid")

    intervention_id: str
    run_id: str
    raised_at: datetime
    condition: StuckCondition
    guidance: str

    # What was being attempted.
    capability_id: str | None = None
    goal: str = ""
    step_index: int
    step_intent: str = ""

    # Where the session actually is. A URL and title are what an operator
    # orients on first; the screenshot is what they check when those lie.
    url: str = ""
    title: str = ""
    state_summary: str = ""
    screenshot_path: str | None = None
    detail: str = ""


class HumanAction(BaseModel):
    """One thing the operator did while they held the lease."""

    model_config = ConfigDict(extra="forbid")

    at: datetime
    kind: str  # navigate | click | input | select
    url: str = ""
    role: str = ""
    name: str = ""
    value: str | None = None


class InterventionTimeout(Exception):
    """No operator resumed the run within the timeout."""


class Intervention:
    """One escalation: its request, its lease, and the record of the handoff.

    Owns a directory rather than a file because a handoff produces three things
    that belong together -- what was asked, who took it, and what they did.
    """

    def __init__(self, intervention_id: str, root: str | Path = DEFAULT_INTERVENTION_DIR) -> None:
        self.intervention_id = intervention_id
        self._dir = Path(root) / intervention_id
        self.lease = LeaseStore(self._dir / "lease.json")

    @property
    def directory(self) -> Path:
        return self._dir

    @property
    def request_path(self) -> Path:
        return self._dir / "request.json"

    @property
    def actions_path(self) -> Path:
        return self._dir / "human-actions.jsonl"

    @property
    def resolved_path(self) -> Path:
        return self._dir / "resolved.json"

    # -- raising -----------------------------------------------------------
    def raise_request(self, request: InterventionRequest) -> Path:
        """Write the request and stop automation acting on the session.

        The lease moves to HUMAN *before* the request is announced as pending,
        not after. Ordering matters: the opposite order leaves a window where an
        operator can take control while automation still believes it may act.
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        self.lease.write(
            ControlLease(
                owner=LeaseOwner.HUMAN,
                holder_id=UNCLAIMED,
                since=datetime.now(UTC),
                reason=f"{request.condition.value}: {request.detail or request.step_intent}",
            )
        )
        self.request_path.write_text(request.model_dump_json(indent=2), encoding="utf-8")
        return self.request_path

    def read_request(self) -> InterventionRequest:
        return InterventionRequest.model_validate_json(
            self.request_path.read_text(encoding="utf-8")
        )

    # -- the operator side -------------------------------------------------
    def take(self, holder_id: str) -> ControlLease:
        """An operator claims the session. Raises if someone else already has it."""
        return self.lease.acquire(
            LeaseOwner.HUMAN, holder_id, reason=f"taken by {holder_id}"
        )

    def resume(self, holder_id: str, step_disposition: str = RETRY_STEP) -> ControlLease:
        """Hand control back, saying what the run should do with the stuck step.

        This argument exists because the obvious design is wrong. If the run
        always retries, an operator who *performed* the step by hand has it
        performed twice -- which for a transfer or a submit is not a cosmetic
        problem. If the run always skips, an operator who merely dismissed a
        dialog never gets the step done at all.

        Only the human knows which of those they did, so the human is asked.
        The default is to retry: retrying a step that is already done is
        visible and usually harmless, whereas skipping one that was never done
        produces a run that reports success having quietly missed something.
        """
        if step_disposition not in (RETRY_STEP, STEP_ALREADY_PERFORMED):
            raise ValueError(
                f"step_disposition must be {RETRY_STEP!r} or {STEP_ALREADY_PERFORMED!r}"
            )
        lease = ControlLease(
            owner=LeaseOwner.AUTOMATION,
            holder_id="run",
            since=datetime.now(UTC),
            reason=f"resumed by {holder_id} ({step_disposition})",
        )
        self.lease.write(lease)
        self.resolved_path.write_text(
            json.dumps(
                {
                    "resolved_by": holder_id,
                    "at": datetime.now(UTC).isoformat(),
                    "step_disposition": step_disposition,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return lease

    def read_disposition(self) -> str:
        """What the operator said to do with the step; retry if they said nothing."""
        if not self.resolved_path.exists():
            return RETRY_STEP
        try:
            payload = json.loads(self.resolved_path.read_text(encoding='utf-8'))
        except json.JSONDecodeError:
            return RETRY_STEP
        disposition = payload.get('step_disposition', RETRY_STEP)
        if disposition not in (RETRY_STEP, STEP_ALREADY_PERFORMED):
            return RETRY_STEP
        return disposition

    # -- the run side ------------------------------------------------------
    def wait_for_resume(
        self,
        timeout_s: float = DEFAULT_WAIT_TIMEOUT_S,
        poll_interval_s: float = POLL_INTERVAL_S,
        sleep=time.sleep,
        now=time.monotonic,
    ) -> ControlLease:
        """Block until an operator returns the lease to automation.

        `sleep` and `now` are injected so this is testable without spending the
        wall-clock time it is designed to spend.
        """
        deadline = now() + timeout_s
        while True:
            lease = self.lease.read()
            if lease.owner is LeaseOwner.AUTOMATION:
                return lease
            if now() >= deadline:
                raise InterventionTimeout(
                    f"no operator resumed {self.intervention_id!r} within {timeout_s:.0f}s; "
                    f"control is still held by {lease.holder_id!r}"
                )
            sleep(poll_interval_s)

    def write_human_actions(self, actions: list[HumanAction]) -> Path:
        """Persist what the operator did, one per line, alongside the request."""
        self._dir.mkdir(parents=True, exist_ok=True)
        with self.actions_path.open("a", encoding="utf-8") as handle:
            for action in actions:
                handle.write(action.model_dump_json() + "\n")
        return self.actions_path


def make_intervention_id(run_id: str, step_index: int) -> str:
    """Stable and sortable: one run can escalate more than once."""
    return f"{run_id}-step{step_index:02d}"


def pending_interventions(root: str | Path = DEFAULT_INTERVENTION_DIR) -> list[Intervention]:
    """Every intervention still waiting on a person, oldest first."""
    base = Path(root)
    if not base.exists():
        return []
    found = []
    for directory in sorted(base.iterdir()):
        if not (directory / "request.json").exists():
            continue
        intervention = Intervention(directory.name, root=base)
        if intervention.lease.read().owner is LeaseOwner.HUMAN:
            found.append(intervention)
    return found


def request_from_stuck(
    condition: StuckCondition,
    run_id: str,
    step_index: int,
    **context,
) -> InterventionRequest:
    """Build a request, filling in the operator guidance the condition implies."""
    return InterventionRequest(
        intervention_id=make_intervention_id(run_id, step_index),
        run_id=run_id,
        raised_at=datetime.now(UTC),
        condition=condition,
        guidance=OPERATOR_GUIDANCE[condition],
        step_index=step_index,
        **context,
    )


__all__ = [
    "DEFAULT_INTERVENTION_DIR",
    "RETRY_STEP",
    "STEP_ALREADY_PERFORMED",
    "HumanAction",
    "Intervention",
    "InterventionRequest",
    "InterventionTimeout",
    "make_intervention_id",
    "pending_interventions",
    "request_from_stuck",
]
