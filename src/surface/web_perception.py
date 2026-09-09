"""Structural perception via the Chrome accessibility tree (over CDP).

Two kinds of node reach the flat element list: **interactive** (button, link,
textbox, ...) which get a live handle, and **readable** (StaticText, heading)
which carry their text in `value` and have none. Only interactive nodes count
toward the coverage gate.

CDP hands back a `backendDOMNodeId` with no supported route to a Playwright
`Locator`, so each interactive node is stamped with a transient `data-cua-ref`
attribute to address it. The stamp is an address, not a perception source —
clicks still go through Playwright's real actionability checks.

Accessible names are often empty on legacy markup, so one JS pass per frame
recovers a name from labels / aria / placeholder / submit value / preceding text.
"""

import contextlib
import hashlib
from datetime import UTC, datetime
from typing import Any

from playwright.sync_api import CDPSession, Frame, Page
from playwright.sync_api import Error as PlaywrightError

from src.surface.base import TargetNotFoundError
from src.surface.coverage import compute_coverage
from src.types import BBox, PerceptionCoverage, ResolvedTarget, UIElement, UIState

# Roles a human operator would click or type into. These get handles.
INTERACTIVE_ROLES = {
    "button",
    "checkbox",
    "combobox",
    "link",
    "listbox",
    "menuitem",
    "menuitemcheckbox",
    "menuitemradio",
    "option",
    "radio",
    "searchbox",
    "slider",
    "spinbutton",
    "switch",
    "tab",
    "textbox",
}

# Leaf text roles only. `cell` / `paragraph` would duplicate their StaticText
# children's strings.
READABLE_ROLES = {"StaticText", "heading"}

# Cap on readable elements per frame, to keep the planner prompt bounded on
# text-dense pages. Interactive elements are never capped.
MAX_READABLE_ELEMENTS = 60

# Shortest text worth exposing as a readable element.
MIN_READABLE_LENGTH = 2

# The transient attribute used to address a perceived node from Playwright.
REF_ATTRIBUTE = "data-cua-ref"

# Per frame: recover a name for unnamed nodes, read back each control's value.
SYNTHESIZE_NAMES_JS = """
() => {
  const nearestPrecedingText = (element) => {
    let node = element;
    let hops = 0;
    while (node && hops < 4) {
      let sibling = node.previousElementSibling;
      while (sibling) {
        const text = (sibling.innerText || sibling.textContent || '').trim();
        if (text) return text.slice(0, 80);
        sibling = sibling.previousElementSibling;
      }
      node = node.parentElement;
      hops += 1;
    }
    return '';
  };

  const results = {};
  document.querySelectorAll('[data-cua-ref]').forEach((element) => {
    const ref = element.getAttribute('data-cua-ref');
    const inputType = (element.getAttribute('type') || '').toLowerCase();

    let label = '';
    let source = '';

    if (element.labels && element.labels.length) {
      label = element.labels[0].innerText.trim();
      source = 'label';
    }
    if (!label && element.getAttribute('aria-label')) {
      label = element.getAttribute('aria-label').trim();
      source = 'aria-label';
    }
    if (!label && element.getAttribute('aria-labelledby')) {
      const referenced = document.getElementById(element.getAttribute('aria-labelledby'));
      if (referenced) { label = referenced.innerText.trim(); source = 'aria-labelledby'; }
    }
    if (!label && element.getAttribute('placeholder')) {
      label = element.getAttribute('placeholder').trim();
      source = 'placeholder';
    }
    // <input type=submit value="Log In"> has no innerText; its value is its label.
    if (!label && element.tagName === 'INPUT'
        && ['submit', 'button', 'reset'].includes(inputType)) {
      label = (element.getAttribute('value') || '').trim();
      source = 'value';
    }
    if (!label) {
      label = (element.innerText || '').trim();
      if (label) source = 'text';
    }
    if (!label) {
      label = nearestPrecedingText(element);
      if (label) source = 'nearby-text';
    }

    // Never report a password's contents as a value.
    const isSecret = inputType === 'password';
    const rawValue = (element.value === undefined || element.value === null)
      ? '' : String(element.value);

    results[ref] = {
      label: label.slice(0, 80),
      source: source,
      value: isSecret ? '' : rawValue.slice(0, 120),
    };
  });
  return results;
}
"""

# Refs restart at e1 each pass, so a leftover stamp would collide with a ref now
# owned by a different node (strict-mode violation on click). Wipe before stamping.
CLEAR_REFS_JS = """
() => {
  document.querySelectorAll('[data-cua-ref]')
    .forEach((element) => element.removeAttribute('data-cua-ref'));
}
"""

# Visible text per frame, used to build the no-op detection digest.
VISIBLE_TEXT_JS = "() => document.body ? document.body.innerText : ''"


def _ax_property_map(node: dict[str, Any]) -> dict[str, Any]:
    """Flatten an AX node's `properties` list into a plain dict."""
    properties: dict[str, Any] = {}
    for entry in node.get("properties", []):
        value = entry.get("value", {}).get("value")
        if value is not None:
            properties[entry["name"]] = value
    return properties


def _order_ax_nodes(
    nodes: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, dict[str, Any]]]:
    """Return AX nodes in document order, a child-id -> parent-id map, and an
    id -> node lookup.

    `getFullAXTree` returns a flat list whose order is not guaranteed to be
    document order, so we walk the tree ourselves from the root. The parent map
    is what lets us compute `parent_ref` later, and the lookup is needed by the
    ancestor walks — building it here means the caller does not rebuild it.
    """
    nodes_by_id = {node["nodeId"]: node for node in nodes}

    parent_of: dict[str, str] = {}
    for node in nodes:
        for child_id in node.get("childIds", []):
            parent_of[child_id] = node["nodeId"]

    # The root is the one node nobody claims as a child.
    roots = [node["nodeId"] for node in nodes if node["nodeId"] not in parent_of]
    ordered: list[dict[str, Any]] = []

    def visit(node_id: str) -> None:
        node = nodes_by_id.get(node_id)
        if node is None:
            return
        ordered.append(node)
        for child_id in node.get("childIds", []):
            visit(child_id)

    for root_id in roots:
        visit(root_id)

    return ordered, parent_of, nodes_by_id


def _quad_to_bbox(quad: list[float]) -> BBox:
    """Convert a CDP box-model content quad (4 corner points) to an axis-aligned box."""
    xs = quad[0::2]
    ys = quad[1::2]
    return BBox(x=min(xs), y=min(ys), width=max(xs) - min(xs), height=max(ys) - min(ys))


def interactive_elements(elements: list[UIElement]) -> list[UIElement]:
    """The subset that can be acted on. Coverage is judged on these only."""
    return [element for element in elements if element.role in INTERACTIVE_ROLES]


class WebPerception:
    """Reads the accessibility tree of a live page into `UIState` snapshots."""

    def __init__(self, page: Page, cdp: CDPSession, min_interactive: int, max_unnamed_ratio: float):
        self._page = page
        self._cdp = cdp
        self._min_interactive = min_interactive
        self._max_unnamed_ratio = max_unnamed_ratio
        # Refs are snapshot-scoped, so this map is rebuilt on every perceive().
        self._handles_by_ref: dict[str, Any] = {}

    # -- public API (PerceptionProvider protocol) --------------------------- #

    def perceive(self, screenshot_path: str | None = None) -> UIState:
        """Capture the page as a flat, surface-agnostic `UIState`."""
        if screenshot_path:
            self._page.screenshot(path=screenshot_path, full_page=False)

        # Populate CDP's frontend node map; required before backend ids can be
        # pushed. `pierce` includes iframe and shadow content.
        self._cdp.send("DOM.getDocument", {"depth": -1, "pierce": True})

        self._handles_by_ref = {}
        elements: list[UIElement] = []
        empty_frames: list[str] = []
        text_parts: list[str] = []
        ref_counter = 0

        for scope, frame_id, frame in self._frames():
            frame_elements, ref_counter = self._perceive_frame(scope, frame_id, frame, ref_counter)
            # A child frame that yields nothing usually means its tree failed to
            # load, not that it is genuinely empty. Report it to the gate.
            if not interactive_elements(frame_elements) and scope != "main":
                empty_frames.append(scope)
            elements.extend(frame_elements)
            text_parts.append(self._frame_text(frame))

        coverage: PerceptionCoverage = compute_coverage(
            elements=interactive_elements(elements),
            empty_frames=empty_frames,
            min_interactive_elements=self._min_interactive,
            max_unnamed_ratio=self._max_unnamed_ratio,
        )

        return UIState(
            url=self._page.url,
            title=self._page.title(),
            elements=elements,
            text_digest=self._digest("\n".join(text_parts)),
            coverage=coverage,
            screenshot_path=screenshot_path,
            captured_at=datetime.now(UTC),
        )

    def resolve(self, target_ref: str, state: UIState) -> ResolvedTarget:
        """Turn a ref from `state` back into an element plus (if any) a live handle.

        Readable text elements legitimately have no handle. Deciding whether a
        handle is required belongs to the executor, which knows the action, so
        this only fails when the ref is unknown.
        """
        for element in state.elements:
            if element.ref == target_ref:
                handle = element.handle or self._handles_by_ref.get(target_ref)
                return ResolvedTarget(
                    ref=target_ref, element=element, handle=handle, resolved_by="ref"
                )

        known = ", ".join(element.ref for element in state.elements) or "<none>"
        raise TargetNotFoundError(
            f"ref {target_ref!r} is not in this snapshot. Known refs: {known}"
        )

    # -- internals --------------------------------------------------------- #

    def _frames(self) -> list[tuple[str, str | None, Frame]]:
        """Pair each Playwright frame with its CDP frame id and a scope key.

        Playwright does not expose CDP frame ids, so we match on URL from
        `Page.getFrameTree`. The main frame is queried without a frame id, which
        is the better-supported path.
        """
        frame_ids_by_url: dict[str, str] = {}

        def collect(tree_node: dict[str, Any]) -> None:
            frame = tree_node["frame"]
            frame_ids_by_url.setdefault(frame.get("url", ""), frame["id"])
            for child in tree_node.get("childFrames", []):
                collect(child)

        try:
            tree = self._cdp.send("Page.getFrameTree")
            collect(tree["frameTree"])
        except (PlaywrightError, KeyError):
            # Without the tree we can still perceive the main frame.
            frame_ids_by_url = {}

        paired: list[tuple[str, str | None, Frame]] = []
        for index, frame in enumerate(self._page.frames):
            if index == 0:
                paired.append(("main", None, frame))
                continue
            scope = f"frame:{frame.name}" if frame.name else f"frame:{index}"
            paired.append((scope, frame_ids_by_url.get(frame.url), frame))
        return paired

    def _perceive_frame(
        self, scope: str, frame_id: str | None, frame: Frame, ref_counter: int
    ) -> tuple[list[UIElement], int]:
        """Read one frame's accessibility tree into `UIElement`s."""
        # Wipe the previous snapshot's stamps first: refs restart at e1 every
        # pass, so any survivor would collide with a ref now owned by a
        # different node. See CLEAR_REFS_JS.
        self._clear_refs(frame)

        # A child frame whose CDP id could not be matched is *skipped*, not
        # queried. `Accessibility.getFullAXTree` with no `frameId` returns the
        # main frame's tree, so falling through here would perceive the main
        # document a second time under this frame's scope -- and because the
        # second pass re-stamps `data-cua-ref` on the very same DOM nodes, it
        # destroys every ref the first pass handed out. The planner then picks a
        # main-frame ref that no longer resolves and every action times out.
        #
        # Found on a real login page carrying a Cloudflare Turnstile iframe:
        # 15 elements perceived twice, `e4` (a textbox) overwritten by `e19`,
        # and `Locator.fill` timing out on all three attempts.
        #
        # Reporting it as unreadable is the honest outcome, and the coverage
        # gate already knows what to do with an empty frame.
        if scope != "main" and frame_id is None:
            return [], ref_counter

        params: dict[str, Any] = {}
        if frame_id is not None:
            params["frameId"] = frame_id

        try:
            response = self._cdp.send("Accessibility.getFullAXTree", params)
        except PlaywrightError:
            # A frame we cannot read is reported as empty, which trips the
            # coverage gate rather than silently pretending the page is fine.
            return [], ref_counter

        ordered_nodes, parent_of, nodes_by_id = _order_ax_nodes(response.get("nodes", []))

        # Pass 1: choose nodes and assign refs, in document order.
        interactive: list[tuple[str, dict[str, Any]]] = []
        readable: list[tuple[str, dict[str, Any]]] = []
        ref_by_ax_id: dict[str, str] = {}

        for node in ordered_nodes:
            if node.get("ignored"):
                continue
            role = (node.get("role") or {}).get("value", "")
            text = ((node.get("name") or {}).get("value") or "").strip()

            if role in INTERACTIVE_ROLES:
                if node.get("backendDOMNodeId") is None:
                    continue
            elif role in READABLE_ROLES:
                if len(text) < MIN_READABLE_LENGTH or len(readable) >= MAX_READABLE_ELEMENTS:
                    continue
                if self._echoes_ancestor(node, text, parent_of, ref_by_ax_id, nodes_by_id):
                    continue
            else:
                continue

            ref_counter += 1
            ref = f"e{ref_counter}"
            ref_by_ax_id[node["nodeId"]] = ref
            (interactive if role in INTERACTIVE_ROLES else readable).append((ref, node))

        if not interactive and not readable:
            return [], ref_counter

        # Only interactive nodes need addressing; readable ones are read from the
        # snapshot itself.
        self._stamp_refs(interactive)
        synthesized = self._synthesize_names(frame) if interactive else {}

        elements: list[UIElement] = []

        for ref, node in interactive:
            role = (node.get("role") or {}).get("value", "")
            ax_name = ((node.get("name") or {}).get("value") or "").strip()
            properties = _ax_property_map(node)
            recovered = synthesized.get(ref, {})

            value = (node.get("value") or {}).get("value")
            if value in (None, ""):
                value = recovered.get("value") or None

            handle = frame.locator(f'[{REF_ATTRIBUTE}="{ref}"]')
            self._handles_by_ref[ref] = handle

            elements.append(
                UIElement(
                    ref=ref,
                    role=role,
                    name=ax_name or str(recovered.get("label", "")).strip(),
                    value=str(value) if value else None,
                    bbox=self._bbox(node.get("backendDOMNodeId")),
                    enabled=not bool(properties.get("disabled", False)),
                    focused=bool(properties.get("focused", False)),
                    parent_ref=self._nearest_ancestor_ref(node["nodeId"], parent_of, ref_by_ax_id),
                    scope=scope,
                    source="structural",
                    confidence=1.0,
                    handle=handle,
                )
            )

        for ref, node in readable:
            role = (node.get("role") or {}).get("value", "")
            text = ((node.get("name") or {}).get("value") or "").strip()
            elements.append(
                UIElement(
                    ref=ref,
                    role=role,
                    # Same string: name for the element table, value for READ.
                    name=text[:120],
                    value=text,
                    bbox=self._bbox(node.get("backendDOMNodeId")),
                    enabled=True,
                    focused=False,
                    parent_ref=self._nearest_ancestor_ref(node["nodeId"], parent_of, ref_by_ax_id),
                    scope=scope,
                    source="structural",
                    confidence=1.0,
                    handle=None,
                )
            )

        # Restore document order across both kinds; refs were assigned in it.
        elements.sort(key=lambda element: int(element.ref[1:]))
        return elements, ref_counter

    def _stamp_refs(self, selected: list[tuple[str, dict[str, Any]]]) -> None:
        """Write `data-cua-ref` onto each interactive node so Playwright can address it."""
        if not selected:
            return
        backend_ids = [node["backendDOMNodeId"] for _, node in selected]
        try:
            pushed = self._cdp.send(
                "DOM.pushNodesByBackendIdsToFrontend", {"backendNodeIds": backend_ids}
            )
        except PlaywrightError:
            return

        for (ref, _), node_id in zip(selected, pushed.get("nodeIds", []), strict=False):
            if not node_id:
                continue
            try:
                self._cdp.send(
                    "DOM.setAttributeValue",
                    {"nodeId": node_id, "name": REF_ATTRIBUTE, "value": ref},
                )
            except PlaywrightError:
                # A node that vanished between snapshot and stamp simply ends up
                # without a working handle; the executor reports that clearly.
                continue

    @staticmethod
    def _clear_refs(frame: Frame) -> None:
        """Remove every ref stamp left behind by the previous snapshot.

        A frame we cannot script is also one we cannot stamp, so there is
        nothing stale in it to clear.
        """
        with contextlib.suppress(PlaywrightError):
            frame.evaluate(CLEAR_REFS_JS)

    def _synthesize_names(self, frame: Frame) -> dict[str, dict[str, Any]]:
        """Recover names/values for stamped nodes the accessibility tree left bare."""
        try:
            return frame.evaluate(SYNTHESIZE_NAMES_JS) or {}
        except PlaywrightError:
            return {}

    def _bbox(self, backend_node_id: int | None) -> BBox | None:
        """Box model for a node, or None when it is not rendered."""
        if backend_node_id is None:
            return None
        try:
            model = self._cdp.send("DOM.getBoxModel", {"backendNodeId": backend_node_id})
        except PlaywrightError:
            return None
        content = model.get("model", {}).get("content")
        if not content or len(content) < 8:
            return None
        return _quad_to_bbox(content)

    @staticmethod
    def _echoes_ancestor(
        node: dict[str, Any],
        text: str,
        parent_of: dict[str, str],
        ref_by_ax_id: dict[str, str],
        nodes_by_id: dict[str, dict[str, Any]],
    ) -> bool:
        """True when this text is just its container's label repeated.

        A link's accessible name comes from the StaticText inside it, so the
        accessibility tree reports both `link "About Us"` and
        `StaticText "About Us"`. Keeping both doubles the element list with
        strings the planner already sees, and on a nav-heavy page the duplicates
        crowd out the content a READ actually needs. The interactive node is the
        useful one, so the echo is dropped.
        """
        ancestor_id = parent_of.get(node["nodeId"])
        while ancestor_id is not None:
            if ancestor_id in ref_by_ax_id:
                ancestor = nodes_by_id.get(ancestor_id, {})
                ancestor_name = ((ancestor.get("name") or {}).get("value") or "").strip()
                return ancestor_name.casefold() == text.casefold()
            ancestor_id = parent_of.get(ancestor_id)
        return False

    @staticmethod
    def _nearest_ancestor_ref(
        ax_node_id: str, parent_of: dict[str, str], ref_by_ax_id: dict[str, str]
    ) -> str | None:
        """Walk up the AX tree to the first ancestor that is itself in the flat list."""
        current = parent_of.get(ax_node_id)
        while current is not None:
            if current in ref_by_ax_id:
                return ref_by_ax_id[current]
            current = parent_of.get(current)
        return None

    @staticmethod
    def _frame_text(frame: Frame) -> str:
        try:
            return frame.evaluate(VISIBLE_TEXT_JS) or ""
        except PlaywrightError:
            return ""

    @staticmethod
    def _digest(text: str) -> str:
        """Whitespace-normalized hash of visible text, for no-op detection.

        Hashed rather than stored raw so a `StepRecord` stays small, and
        normalized so incidental reflow does not read as a change.
        """
        normalized = " ".join(text.split())
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
