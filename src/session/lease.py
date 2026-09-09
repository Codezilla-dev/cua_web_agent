"""Who is allowed to drive the browser right now.

The brief's requirement is that a human takes control of *the same live
session* the automation was using, not a fresh one. That only means anything if
"in control" is a fact the system can state, so control is a single-writer
**lease** rather than a convention: exactly one owner at a time, written down,
and checked at the point where actions actually reach the browser.

Two properties make this worth the file it is stored in:

**It is a question you can ask.** `LeaseStore.read()` answers "who is in
control" with a value, not an inference from which process happens to be
running. `cua operator status` is that call and nothing else.

**It is enforced, not announced.** `require_automation()` is called inside the
surface's action executor. An automation step taken while a human holds the
lease does not race the human -- it raises. That is the difference between
ceding control and merely intending to.

The lease lives in a file because the two parties are different processes: the
run holds the browser, and the operator CLI is invoked separately, possibly
minutes later. A file is the smallest thing both can see. It is not a
distributed lock and does not pretend to be -- a single machine, two local
processes, and a compare-then-write that is honest about being non-atomic
(`acquire` refuses a lease that is already held by someone else, which is the
case that actually occurs; two operators racing for the same intervention in
the same millisecond is not a scenario this system has).
"""

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class LeaseOwner(StrEnum):
    """Who holds the right to act on the session."""

    AUTOMATION = "automation"
    HUMAN = "human"


# Raising an intervention must stop automation *immediately*, before any
# operator has seen it -- otherwise there is a window in which the run keeps
# acting on a session it has already declared itself stuck on. That produces a
# real state: owned by HUMAN, claimed by nobody. This is the holder_id that
# state uses, and `acquire` treats it as free rather than as a rival operator.
UNCLAIMED = "awaiting-operator"


class ControlDeniedError(Exception):
    """An action was attempted by a party that does not hold the lease."""


class LeaseConflictError(Exception):
    """The lease is already held by someone else. The HTTP analogue is 409."""


class ControlLease(BaseModel):
    """The current holder of a session's control, and why."""

    model_config = ConfigDict(extra="forbid")

    owner: LeaseOwner
    holder_id: str
    since: datetime
    reason: str = ""


class LeaseStore:
    """A lease persisted next to its intervention, readable by both processes."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def read(self) -> ControlLease:
        """Current lease. Absent means automation holds it: a run that has never
        escalated is running, and that is the only state that can produce a
        missing file."""
        if not self._path.exists():
            return ControlLease(
                owner=LeaseOwner.AUTOMATION,
                holder_id="run",
                since=datetime.now(UTC),
                reason="no handoff has occurred",
            )
        return ControlLease.model_validate_json(self._path.read_text(encoding="utf-8"))

    def write(self, lease: ControlLease) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(lease.model_dump_json(indent=2), encoding="utf-8")

    def acquire(self, owner: LeaseOwner, holder_id: str, reason: str = "") -> ControlLease:
        """Take the lease, refusing if a *different* holder already has it.

        Re-acquiring your own lease is allowed and idempotent, so an operator who
        runs `take` twice gets the lease rather than an error about themselves.
        """
        current = self.read()
        held_by_someone_else = (
            current.owner == owner
            and current.holder_id != holder_id
            and current.holder_id != UNCLAIMED
        )
        if held_by_someone_else:
            raise LeaseConflictError(
                f"{owner.value} control is already held by {current.holder_id!r} "
                f"since {current.since.isoformat()}"
            )
        lease = ControlLease(
            owner=owner, holder_id=holder_id, since=datetime.now(UTC), reason=reason
        )
        self.write(lease)
        return lease

    def require_automation(self) -> None:
        """Raise unless automation may act. Called at the surface boundary.

        Deliberately not `is_automation() -> bool`: a caller that forgets to
        check a boolean proceeds, and a caller that forgets to call this does
        not compile a check it never wrote. Raising is the safer default for the
        one function standing between a human's hands and a robot's.
        """
        lease = self.read()
        if lease.owner is not LeaseOwner.AUTOMATION:
            raise ControlDeniedError(
                f"{lease.holder_id!r} holds {lease.owner.value} control since "
                f"{lease.since.isoformat()}: {lease.reason or 'no reason recorded'}"
            )


class SessionControl:
    """The gate the surface is built with, before any handoff has happened.

    The surface needs its control gate at construction time, but the lease that
    will actually govern a handoff does not exist until something goes wrong --
    it is created with the intervention. This is the indirection that resolves
    that ordering: the run hands the surface a `SessionControl` on day one, and
    binds a real lease to it at the moment it escalates.

    Unbound means unrestricted, which is correct: a run that has not escalated
    has nobody to yield to.
    """

    def __init__(self) -> None:
        self._lease: LeaseStore | None = None

    def bind(self, lease: LeaseStore) -> None:
        """Put this session under the given lease's control."""
        self._lease = lease

    def release(self) -> None:
        """Stop deferring to a lease -- the handoff is over."""
        self._lease = None

    @property
    def bound(self) -> bool:
        return self._lease is not None

    def require_automation(self) -> None:
        if self._lease is not None:
            self._lease.require_automation()


def read_lease_owner(path: str | Path) -> str:
    """Convenience for the CLI's status line."""
    return LeaseStore(path).read().owner.value


__all__ = [
    "UNCLAIMED",
    "ControlDeniedError",
    "ControlLease",
    "LeaseConflictError",
    "LeaseOwner",
    "LeaseStore",
    "SessionControl",
    "read_lease_owner",
]
