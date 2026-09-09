"""Desktop surface — a deliberate stub, to prove the seam is real.

Nothing above `Surface` would change to drive a native app; only this file
would be filled in. UI Automation on Windows, AXUIElement on macOS — both
expose the same role + name + state model, which is why `UIElement` carries
no web-specific fields.

Not implemented, and intentionally not faked.
"""

from src.types import ActResult, Intent, ResolvedTarget, SettleResult, UIState

_NOT_IMPLEMENTED = (
    "The desktop surface is a documented seam, not an implementation. "
    "See the module docstring for how it would be built."
)


class DesktopPerception:
    """Would read the OS accessibility tree (UI Automation / AXUIElement)."""

    def perceive(self, screenshot_path: str | None = None) -> UIState:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def resolve(self, target_ref: str, state: UIState) -> ResolvedTarget:
        raise NotImplementedError(_NOT_IMPLEMENTED)


class DesktopActions:
    """Would drive the native control patterns (Invoke, Value, SelectionItem)."""

    def execute(self, intent: Intent, target: ResolvedTarget | None) -> ActResult:
        raise NotImplementedError(_NOT_IMPLEMENTED)


class DesktopSettle:
    """Would wait for the application's UI thread to go idle."""

    def wait_until_settled(self, timeout_seconds: float) -> SettleResult:
        raise NotImplementedError(_NOT_IMPLEMENTED)


class DesktopSurface:
    """A native application target. Satisfies the `Surface` protocol; does nothing."""

    def __init__(self, application_path: str):
        self.application_path = application_path
        self.perception = DesktopPerception()
        self.actions = DesktopActions()
        self.settle = DesktopSettle()

    def start(self) -> None:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def stop(self) -> None:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def goto(self, url: str) -> None:
        raise NotImplementedError(_NOT_IMPLEMENTED)
