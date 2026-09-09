"""Recording what a human does while they hold the session.

The brief asks for the human's actions to be captured across the handoff. That
is not the same problem as automation logging its own steps: we did not choose
these actions, we are observing someone else's, and we find out about them from
the page rather than from an intent we already had in hand.

Two capture channels, because neither is sufficient alone:

**DOM events**, via a listener installed in the page and an exposed binding
back into Python. This is what sees a click on a link or a value typed into a
field. It is installed both on the current document and, through an init
script, on every document the operator navigates to afterwards -- a recorder
that stops working the moment the human clicks a link would miss most of what
they came to do.

**Frame navigation**, via Playwright's own event. A navigation may be caused by
something the DOM listener never sees (the address bar, a form post, a
redirect), and "where did they end up" is the single most useful fact for
whoever reads this later.

Listeners are registered in the capture phase so that a click on an element
that stops propagation is still recorded. We are auditing, not participating.

**Passwords are dropped at the source.** A field of type `password` records
that it was filled and never what with. The redactor still sweeps everything as
a backstop, but not capturing a secret is strictly better than capturing and
scrubbing it -- the scrub can only remove secrets it was told about, and an
operator may type one this process has never seen.
"""

from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from src.escalation.intervention import HumanAction

# Installed once per document. Guarded by a flag because an init script and the
# explicit first-page evaluation can both reach the same document.
_RECORDER_JS = """
() => {
  if (window.__cuaRecorderInstalled) return;
  window.__cuaRecorderInstalled = true;

  const describe = (el) => {
    if (!el || !el.tagName) return { role: '', name: '' };
    const role = el.getAttribute('role') || el.tagName.toLowerCase();
    const name =
      el.getAttribute('aria-label') ||
      el.getAttribute('name') ||
      (el.labels && el.labels[0] && el.labels[0].innerText) ||
      (el.innerText || '').trim().slice(0, 80) ||
      el.id ||
      '';
    return { role, name: String(name).trim() };
  };

  const send = (payload) => {
    try { window.__cuaRecord(payload); } catch (e) { /* page closing */ }
  };

  document.addEventListener('click', (event) => {
    const { role, name } = describe(event.target);
    send({ kind: 'click', role, name, value: null });
  }, true);

  document.addEventListener('change', (event) => {
    const el = event.target;
    const { role, name } = describe(el);
    // A password field records that it was filled, never with what.
    const secret = el && el.type === 'password';
    const value = secret ? null : String((el && el.value) || '').slice(0, 120);
    send({ kind: el && el.tagName === 'SELECT' ? 'select' : 'input', role, name, value });
  }, true);
}
"""


class WebHumanActionRecorder:
    """Captures the operator's actions on a live page.

    Constructed against the page the automation was already using -- the point
    of the handoff is that it is the same session, so there is no second page to
    attach to.
    """

    def __init__(self, page: Any, redactor: Any = None) -> None:
        self._page = page
        self._redactor = redactor
        self._actions: list[HumanAction] = []
        self._started = False

    def start(self) -> None:
        """Install both capture channels. Safe to call once per handoff."""
        if self._started:
            return
        self._started = True

        # Already exposed from an earlier handoff on this page: the binding
        # survives navigation, so a second take must not fail because of it.
        with suppress(Exception):
            self._page.expose_function("__cuaRecord", self._on_dom_event)

        self._page.add_init_script(_RECORDER_JS)

        # Every frame, not just the main document. The target app serves its
        # search results inside an iframe, so the control an operator is most
        # likely to click during a drift incident lives in a child frame -- a
        # recorder that only instrumented the top document would miss exactly
        # the action it was installed to capture. Failures here are tolerated
        # per frame: a cross-origin frame we cannot script is not a reason to
        # abandon the ones we can.
        for frame in self._frames():
            with suppress(Exception):
                frame.evaluate(_RECORDER_JS)

        self._page.on("framenavigated", self._on_navigation)

    def stop(self) -> list[HumanAction]:
        """Detach and return what was seen, oldest first."""
        if not self._started:
            return []
        self._started = False
        with suppress(Exception):
            self._page.remove_listener("framenavigated", self._on_navigation)
        return list(self._actions)

    def _frames(self) -> list[Any]:
        """Every frame in the page, main document first. Never raises."""
        try:
            return list(self._page.frames)
        except Exception:
            return []

    # -- capture -----------------------------------------------------------
    def _on_dom_event(self, payload: dict[str, Any]) -> None:
        self._record(
            HumanAction(
                at=datetime.now(UTC),
                kind=str(payload.get("kind", "unknown")),
                url=self._safe_url(),
                role=str(payload.get("role") or ""),
                name=self._scrub(str(payload.get("name") or "")) or "",
                value=self._scrub(payload.get("value")),
            )
        )

    def _on_navigation(self, frame: Any) -> None:
        # Sub-frame navigations fire constantly on a page with iframes and are
        # noise for an audit log; the main frame is the operator's actual move.
        try:
            if frame != self._page.main_frame:
                return
            url = frame.url
        except Exception:
            return
        if self._actions and self._actions[-1].kind == "navigate" and self._actions[-1].url == url:
            return
        self._record(HumanAction(at=datetime.now(UTC), kind="navigate", url=url))

    def _record(self, action: HumanAction) -> None:
        self._actions.append(action)

    def _scrub(self, value: str | None) -> str | None:
        if value is None or self._redactor is None:
            return value
        scrubbed = self._redactor.scrub(value)
        return scrubbed if isinstance(scrubbed, str) else value

    def _safe_url(self) -> str:
        try:
            return str(self._page.url)
        except Exception:
            return ""
