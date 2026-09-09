"""The Playwright web surface.

This file and `web_perception.py` are the only places Playwright is imported.
`WebSurface` owns the browser and hands out perception, actions and settle.
"""

import contextlib
import time
from typing import Any

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    ViewportSize,
    sync_playwright,
)
from playwright.sync_api import Error as PlaywrightError

from src.surface.base import ACTIONS_NEEDING_TARGET, ControlGate, SurfaceError
from src.surface.web_handoff import WebHumanActionRecorder
from src.surface.web_perception import WebPerception
from src.types import ActionKind, ActResult, Intent, ResolvedTarget, SettleResult

# Roles whose current content lives in a form value rather than in text.
VALUE_BEARING_ROLES = {"textbox", "searchbox", "spinbutton", "combobox", "listbox"}


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


class WebActions:
    """Executes one `Intent` against the page.

    Operational failures come back as `ActResult(ok=False, error=...)`; only
    misuse (a CLICK with no target) raises.
    """

    def __init__(
        self,
        page: Page,
        action_timeout_ms: int,
        control: ControlGate | None = None,
    ):
        self._page = page
        self._timeout = action_timeout_ms
        self._control = control

    def execute(self, intent: Intent, target: ResolvedTarget | None) -> ActResult:
        started = time.monotonic()

        # The control check comes before anything else, including argument
        # validation. This is the boundary every automated action passes
        # through, so it is the one place where "a human has the session" can be
        # made true rather than merely intended. Raising rather than returning
        # ActResult(ok=False) is deliberate: a denied action is not a step that
        # failed, it is a step that must not be attempted, and a caller that
        # treats failures as retryable must not retry this one.
        if self._control is not None:
            self._control.require_automation()

        if intent.action in ACTIONS_NEEDING_TARGET and target is None:
            raise SurfaceError(
                f"{intent.action} requires a target, but target_ref was not resolved"
            )

        try:
            read_value = self._dispatch(intent, target)
        except PlaywrightError as error:
            # Playwright errors are multi-line; the first line is the useful part.
            first_line = str(error).strip().splitlines()[0]
            return ActResult(
                ok=False, action=intent.action, duration_ms=_elapsed_ms(started), error=first_line
            )

        return ActResult(
            ok=True,
            action=intent.action,
            duration_ms=_elapsed_ms(started),
            read_value=read_value,
        )

    def _dispatch(self, intent: Intent, target: ResolvedTarget | None) -> str | None:
        """Perform the action. Returns a value only for READ."""
        if intent.action is ActionKind.NAVIGATE:
            if not intent.value:
                raise SurfaceError("navigate requires a URL in `value`")
            self._page.goto(intent.value, timeout=self._timeout)
            return None

        if intent.action in (ActionKind.WAIT, ActionKind.DONE):
            # WAIT is satisfied by the settle stage that follows every action;
            # DONE is a declaration, not an interaction.
            return None

        assert target is not None  # guaranteed by the check in execute()

        if intent.action is ActionKind.READ:
            return self._read(target)

        handle = self._require_handle(target, intent.action)

        if intent.action is ActionKind.CLICK:
            handle.click(timeout=self._timeout)
            return None

        if intent.action is ActionKind.TYPE:
            if intent.value is None:
                raise SurfaceError("type requires text in `value`")
            # fill() clears first, so a retry cannot append to a half-typed field.
            handle.fill(intent.value, timeout=self._timeout)
            return None

        if intent.action is ActionKind.SELECT:
            if intent.value is None:
                raise SurfaceError("select requires an option in `value`")
            self._select_option(handle, intent.value)
            return None

        raise SurfaceError(f"unsupported action {intent.action}")

    def _read(self, target: ResolvedTarget) -> str:
        """Read a value without changing anything.

        Readable text elements carry their text in the snapshot and have no
        handle, so they are answered from perception — no DOM round-trip, which
        keeps a READ deterministic.
        """
        if target.handle is None:
            return (target.element.value or target.element.name or "").strip()

        if target.element.role in VALUE_BEARING_ROLES:
            return (target.handle.input_value(timeout=self._timeout) or "").strip()

        return (target.handle.inner_text(timeout=self._timeout) or "").strip()

    def _select_option(self, handle: Any, wanted: str) -> None:
        """Select by visible label, falling back to the option's value attribute.

        Legacy selects often have opaque values ("1", "SAV") behind readable
        labels, and the planner only ever sees the label.
        """
        try:
            handle.select_option(label=wanted, timeout=self._timeout)
        except PlaywrightError:
            handle.select_option(value=wanted, timeout=self._timeout)

    @staticmethod
    def _require_handle(target: ResolvedTarget, action: ActionKind) -> Any:
        if target.handle is None:
            raise SurfaceError(
                f"{action} needs an interactive element, but ref {target.ref!r} "
                f"is a {target.element.role!r} with no handle"
            )
        return target.handle


class WebSettle:
    """Waits for the page to stop changing after an action.

    Two signals, cheapest first: the network going idle, then the rendered text
    holding still across two samples. The second matters because legacy pages
    often finish their network work well before the DOM settles.

    A timeout is reported, not raised. The verify stage that follows is what
    decides whether the step actually succeeded.
    """

    STABILITY_SAMPLE_GAP_MS = 250

    def __init__(self, page: Page):
        self._page = page

    def wait_until_settled(self, timeout_seconds: float) -> SettleResult:
        started = time.monotonic()
        deadline_ms = int(timeout_seconds * 1000)

        try:
            self._page.wait_for_load_state("networkidle", timeout=deadline_ms)
            signal = "network_idle"
        except PlaywrightError:
            signal = "timeout"

        remaining_ms = deadline_ms - _elapsed_ms(started)
        if remaining_ms > self.STABILITY_SAMPLE_GAP_MS * 2:
            if not self._text_is_stable(remaining_ms):
                signal = "timeout"
            elif signal == "network_idle":
                # Both signals agreed; report the stronger of the two.
                signal = "dom_stable"

        return SettleResult(
            settled=signal != "timeout",
            waited_ms=_elapsed_ms(started),
            signal=signal,
        )

    def _text_is_stable(self, budget_ms: int) -> bool:
        """True once two consecutive text samples match within the budget."""
        deadline = time.monotonic() + budget_ms / 1000
        previous = self._safe_text()
        while time.monotonic() < deadline:
            self._page.wait_for_timeout(self.STABILITY_SAMPLE_GAP_MS)
            current = self._safe_text()
            if current == previous:
                return True
            previous = current
        return False

    def _safe_text(self) -> str:
        try:
            return self._page.evaluate("() => document.body ? document.body.innerText : ''") or ""
        except PlaywrightError:
            return ""


class WebSurface:
    """A Chromium browser driven by Playwright. Satisfies the `Surface` protocol."""

    def __init__(
        self,
        headed: bool = True,
        settle_timeout_s: float = 8.0,
        action_timeout_s: float = 15.0,
        min_interactive_elements: int = 3,
        max_unnamed_ratio: float = 0.4,
        viewport_width: int = 1280,
        viewport_height: int = 900,
        control: ControlGate | None = None,
        debug_port: int | None = None,
    ):
        self._headed = headed
        self._settle_timeout_s = settle_timeout_s
        self._action_timeout_ms = int(action_timeout_s * 1000)
        self._min_interactive_elements = min_interactive_elements
        self._max_unnamed_ratio = max_unnamed_ratio
        self._viewport = ViewportSize(width=viewport_width, height=viewport_height)
        # None means "nothing can take the session from us", which is the right
        # default for a run with no escalation path wired in.
        self._control = control
        # When set, the browser also listens on a CDP port. This is how a human
        # reaches the *same* session during a handoff: the operator console is a
        # separate process, and attaching to the live browser is the only way for
        # it to act on the window this run opened -- going through this process's
        # own executor is exactly what the lease forbids. Off unless a handoff is
        # possible; an open debugging port is not a default worth having.
        self._debug_port = debug_port

        self._playwright: Any = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

        # Populated by start(); typed loosely here because they do not exist yet.
        self.perception: Any = None
        self.actions: Any = None
        self.settle: Any = None

    def start(self) -> None:
        """Launch the browser and wire up the three capabilities."""
        self._playwright = sync_playwright().start()
        launch_args = (
            [f"--remote-debugging-port={self._debug_port}"] if self._debug_port else []
        )
        browser = self._playwright.chromium.launch(
            headless=not self._headed, args=launch_args
        )
        context = browser.new_context(viewport=self._viewport)
        page = context.new_page()

        cdp = context.new_cdp_session(page)
        cdp.send("DOM.enable")
        cdp.send("Accessibility.enable")

        self._browser = browser
        self._context = context
        self._page = page
        self.perception = WebPerception(
            page=page,
            cdp=cdp,
            min_interactive=self._min_interactive_elements,
            max_unnamed_ratio=self._max_unnamed_ratio,
        )
        self.actions = WebActions(
            page=page,
            action_timeout_ms=self._action_timeout_ms,
            control=self._control,
        )
        self.settle = WebSettle(page=page)

    def pump(self, seconds: float) -> None:
        """Idle for `seconds` while still dispatching browser events.

        Playwright's sync API delivers events only while the caller is inside
        it. A run that waits out a handoff on `time.sleep` is therefore blind to
        everything the operator does -- which is precisely the thing the handoff
        is supposed to witness. Waiting *through* the page instead keeps the
        event loop turning, so DOM callbacks and navigations still arrive.

        Found by running the handoff end to end and getting an empty
        human-actions.jsonl from a session where a human had demonstrably
        clicked something.
        """
        if self._page is None:
            time.sleep(seconds)
            return
        try:
            self._page.wait_for_timeout(seconds * 1000)
        except PlaywrightError:
            # The page went away mid-handoff; falling back keeps the wait honest.
            time.sleep(seconds)

    def human_recorder(self, redactor: Any = None) -> WebHumanActionRecorder:
        """A recorder attached to the page automation is already using.

        Exposed from the surface rather than constructed by the caller because
        the caller must not hold the Playwright `Page` -- that is the boundary
        `base.py` draws, and a handoff is not a reason to cross it. The recorder
        is the same session by construction: there is no other page to pass.
        """
        if self._page is None:
            raise SurfaceError("cannot record a handoff before start()")
        return WebHumanActionRecorder(self._page, redactor=redactor)

    def stop(self) -> None:
        """Tear everything down. Safe to call more than once."""
        for closer in (self._context, self._browser):
            if closer is not None:
                with contextlib.suppress(PlaywrightError):
                    closer.close()
        if self._playwright is not None:
            self._playwright.stop()

        self._context = self._browser = self._page = self._playwright = None
        self.perception = self.actions = self.settle = None

    def goto(self, url: str) -> None:
        """One-off navigation to load the target. Setup, not a recorded step."""
        if self._page is None:
            raise SurfaceError("surface not started; call start() first")
        self._page.goto(url, timeout=self._action_timeout_ms)

    @property
    def settle_timeout_s(self) -> float:
        return self._settle_timeout_s
