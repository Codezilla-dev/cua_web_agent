"""The `Surface` port — the single platform seam.

Nothing outside `src/surface/` imports Playwright. A `Surface` is `perception`
+ `actions` + `settle`, plus `start` / `stop` and a one-off `goto` for the
initial load (planner navigation goes through `actions`, not `goto`).

Error contract: `resolve()` raises `TargetNotFoundError` for a missing ref;
`execute()` returns `ActResult(ok=False, error=...)` for operational failures
and raises `SurfaceError` only for misuse.
"""

from typing import Protocol, runtime_checkable

from src.types import ActionKind, ActResult, Intent, ResolvedTarget, SettleResult, UIState


class SurfaceError(Exception):
    """Base class for all surface-layer failures."""


class TargetNotFoundError(SurfaceError):
    """`target_ref` is not present in the supplied `UIState`."""


# Actions that cannot run without a resolved target. Shared by the loop (when
# to call resolve()) and WebActions (what to reject), so the two cannot drift.
ACTIONS_NEEDING_TARGET = {
    ActionKind.CLICK,
    ActionKind.TYPE,
    ActionKind.SELECT,
    ActionKind.READ,
}


@runtime_checkable
class PerceptionProvider(Protocol):
    """Produces `UIState` snapshots and turns refs back into live handles."""

    def perceive(self, screenshot_path: str | None = None) -> UIState:
        """Capture the current UI as a `UIState`.

        If `screenshot_path` is given, also write a PNG there and record it on
        `UIState.screenshot_path`. The screenshot and the structural snapshot are
        taken together so they describe the same moment.
        """
        ...

    def resolve(self, target_ref: str, state: UIState) -> ResolvedTarget:
        """Return a `ResolvedTarget` (element + live handle) for `target_ref`
        within `state`. Raise `TargetNotFoundError` if the ref is unknown."""
        ...


@runtime_checkable
class ActionExecutor(Protocol):
    """Executes a single planner `Intent`."""

    def execute(self, intent: Intent, target: ResolvedTarget | None) -> ActResult:
        """Perform `intent`. `target` is the resolved element for CLICK / TYPE /
        SELECT / READ, and `None` for NAVIGATE / WAIT / DONE (NAVIGATE takes its
        URL from `intent.value`).

        Returns an `ActResult`; `ActResult.settle` is left `None` here and filled
        in by the loop after the settle stage runs. Does not verify the
        expectation — that is a separate stage.
        """
        ...


@runtime_checkable
class SettleDetector(Protocol):
    """Waits for the UI to reach a stable state after an action."""

    def wait_until_settled(self, timeout_seconds: float) -> SettleResult:
        """Block until the UI stops changing or `timeout_seconds` elapses.
        `SettleResult.signal` records which condition ended the wait."""
        ...


@runtime_checkable
class ControlGate(Protocol):
    """Whether automation is currently allowed to act on this session.

    A protocol rather than a direct import of `src.session` so the layering
    holds: the surface knows that *something* may withhold permission, not what
    a lease is or where it is stored. The escalation machinery supplies the real
    one; tests supply a two-line fake.
    """

    def require_automation(self) -> None:
        """Return if automation may act; raise if a human holds the session."""
        ...


@runtime_checkable
class Surface(Protocol):
    """A concrete platform target (a web browser, later a desktop app)."""

    perception: PerceptionProvider
    actions: ActionExecutor
    settle: SettleDetector

    @property
    def settle_timeout_s(self) -> float:
        """How long `settle.wait_until_settled` should block.

        A read-only property, not a plain attribute: `WebSurface` exposes it
        the same way, and a plain `float` annotation here would require a
        *settable* attribute for structural typing to match, which is not
        the contract either side wants -- callers read this, they don't set
        it.
        """
        ...

    def start(self) -> None:
        """Launch the underlying platform (e.g. start the browser)."""
        ...

    def stop(self) -> None:
        """Tear everything down. Safe to call more than once."""
        ...

    def goto(self, url: str) -> None:
        """One-off navigation to load the target before the first observe.
        Setup only — not a recorded step."""
        ...
