"""Fault injection via a `?inject=` query parameter.

Not exercised in Phase 1; built now so the error-taxonomy phase has something
to write against. Timing faults are applied here; page-shaped ones (denied,
not found) are returned to the route to render.
"""

import time
from enum import StrEnum

# Long enough to blow past a normal settle timeout without hanging a test run
# forever.
TIMEOUT_SECONDS = 30.0

# Slow but survivable: the kind of latency a retry should absorb.
SLOW_SECONDS = 2.5


class Fault(StrEnum):
    TIMEOUT = "timeout"
    SLOW = "slow"
    NOTFOUND = "notfound"
    MODAL = "modal"
    DENIED = "denied"


def parse_fault(inject: str | None) -> Fault | None:
    """Turn the query value into a `Fault`, ignoring anything unrecognised."""
    if not inject:
        return None
    try:
        return Fault(inject.strip().lower())
    except ValueError:
        return None


def apply_timing_fault(fault: Fault | None) -> None:
    """Block the request for the timing faults. Others are page-shaped."""
    if fault is Fault.TIMEOUT:
        time.sleep(TIMEOUT_SECONDS)
    elif fault is Fault.SLOW:
        time.sleep(SLOW_SECONDS)
