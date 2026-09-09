"""Probe a live page's accessibility tree. Exploratory tool, not a test.

The tree comes from CDP (`Accessibility.getFullAXTree`), since Playwright's
Python `page.accessibility` API was removed. Also captures `aria_snapshot()`
and a DOM name-synthesis pass for controls the tree leaves unnamed.

Writes a timestamped dir under --out:

    screenshot.png        full-page screenshot (the secondary perception signal)
    ax_cdp_full.json      raw CDP Accessibility.getFullAXTree
    ax_cdp_tree.json      the same, rebuilt into a nested role/name/props tree
    aria_snapshot.yaml    locator("body").aria_snapshot()
    interactive_nodes.json flattened actionable AX nodes: role / name / state / path
    dom_controls.json     every actionable DOM element + a synthesized label + attrs
    roles_histogram.json  count of every role in the tree
    frames.json           frames on the page (frameset / iframe reality check)
    summary.txt           human-readable digest (also printed to stdout)

Usage:
    uv run python scripts/explore_ax_tree.py
    uv run python scripts/explore_ax_tree.py --headed
    uv run python scripts/explore_ax_tree.py --url "https://www.saucedemo.com" --out runs/sauce
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

# Windows terminals default to cp1252; our summaries contain non-ASCII. Force UTF-8.
for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(Exception):
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

DEFAULT_URL = (
    "https://parabank.parasoft.com/parabank/index.htm"
    ";jsessionid=AD3BDC26B2A4FC7F1EE76F1531A2824C"
)

# Roles a human operator would click / type into — the candidate set for what
# perception.Observation.nodes will expose to the agent.
INTERACTIVE_ROLES = {
    "link", "button", "textbox", "searchbox", "checkbox", "radio", "combobox",
    "listbox", "option", "menuitem", "menuitemcheckbox", "menuitemradio", "tab",
    "switch", "slider", "spinbutton",
}

# CDP AX property names worth keeping per node.
KEEP_PROPS = (
    "focusable", "focused", "editable", "settable", "disabled", "readonly",
    "required", "checked", "pressed", "expanded", "selected", "level", "invalid",
    "valuetext", "hasPopup",
)


# CDP full AX tree -> nested tree + flat interactive list
def _prop_map(node: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for p in node.get("properties", []):
        val = p.get("value", {}).get("value")
        if val not in (None, False, ""):
            out[p["name"]] = val
    return out


def build_cdp_tree(nodes: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, Counter[str]]:
    by_id = {n["nodeId"]: n for n in nodes}
    hist: Counter[str] = Counter()
    child_ids: set[str] = set()
    for n in nodes:
        child_ids.update(n.get("childIds", []))

    def condense(node_id: str) -> dict[str, Any]:
        n = by_id[node_id]
        role = (n.get("role") or {}).get("value", "")
        name = (n.get("name") or {}).get("value", "")
        hist[role or "<none>"] += 1
        props = _prop_map(n)
        entry: dict[str, Any] = {"role": role, "name": name}
        kept = {k: props[k] for k in KEEP_PROPS if k in props}
        if kept:
            entry["state"] = kept
        if n.get("backendDOMNodeId") is not None:
            entry["backendDOMNodeId"] = n["backendDOMNodeId"]
        kids = [condense(c) for c in n.get("childIds", []) if c in by_id]
        if kids:
            entry["children"] = kids
        return entry

    roots = [n["nodeId"] for n in nodes if n["nodeId"] not in child_ids]
    if not roots:
        return None, hist
    tree = condense(roots[0])
    return tree, hist


def flatten_interactive(tree: dict[str, Any], path: str = "") -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    role, name = tree.get("role", ""), tree.get("name", "")
    here = f"{path}/{role}" + (f"[{name!r}]" if name else "")
    if role in INTERACTIVE_ROLES:
        out.append(
            {
                "role": role,
                "name": name,
                "named": bool(name and name.strip()),
                "state": tree.get("state", {}),
                "backendDOMNodeId": tree.get("backendDOMNodeId"),
                "path": here,
            }
        )
    for child in tree.get("children", []):
        out.extend(flatten_interactive(child, here))
    return out


# DOM-side control inventory + name synthesis (prototype for perception)
DOM_CONTROLS_JS = r"""
() => {
  const sel =
    'a[href], button, input:not([type=hidden]), select, textarea, [role=button], [tabindex]';
  const nearestText = (el) => {
    // walk previous siblings, then up a level, collecting visible text
    let hops = 0, node = el;
    while (node && hops < 4) {
      let sib = node.previousElementSibling;
      while (sib) {
        const t = (sib.innerText || sib.textContent || '').trim();
        if (t) return t.slice(0, 60);
        sib = sib.previousElementSibling;
      }
      node = node.parentElement; hops++;
    }
    return '';
  };
  return [...document.querySelectorAll(sel)].map((el) => {
    const labelledBy = el.getAttribute('aria-labelledby');
    let labelText = '';
    if (el.labels && el.labels.length) labelText = el.labels[0].innerText.trim();
    else if (labelledBy) {
      const l = document.getElementById(labelledBy);
      if (l) labelText = l.innerText.trim();
    }
    return {
      tag: el.tagName.toLowerCase(),
      type: el.getAttribute('type') || '',
      id: el.id || '',
      name: el.getAttribute('name') || '',
      placeholder: el.getAttribute('placeholder') || '',
      ariaLabel: el.getAttribute('aria-label') || '',
      value: (el.value || '').toString().slice(0, 40),
      text: (el.innerText || el.textContent || '').trim().slice(0, 60),
      href: el.getAttribute('href') || '',
      domLabel: labelText,
      synthesizedLabel: labelText || el.getAttribute('aria-label') || el.getAttribute('placeholder')
                        || (el.innerText || '').trim().slice(0, 60) || nearestText(el),
    };
  });
}
"""


# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--headed", action="store_true", help="show the browser window")
    ap.add_argument("--out", default="runs/ax-probe", help="parent output dir")
    ap.add_argument("--timeout", type=int, default=20_000, help="nav timeout (ms)")
    args = ap.parse_args()

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(args.out) / stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    def w(name: str, obj: Any) -> None:
        text = obj if isinstance(obj, str) else json.dumps(obj, indent=2, ensure_ascii=False)
        (out_dir / name).write_text(text, encoding="utf-8")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()

        print(f"-> navigating to {args.url}")
        page.goto(args.url, wait_until="domcontentloaded", timeout=args.timeout)
        # ParaBank keepalive pings can keep the network busy; not fatal if this times out.
        with contextlib.suppress(Exception):
            page.wait_for_load_state("networkidle", timeout=8_000)

        landed_url, title = page.url, page.title()
        print(f"   landed on: {landed_url}")
        print(f"   title:     {title!r}")

        page.screenshot(path=str(out_dir / "screenshot.png"), full_page=True)

        # ARIA snapshot (terse) ------------------------------------------- #
        try:
            w("aria_snapshot.yaml", page.locator("body").aria_snapshot())
        except Exception as exc:
            print(f"   (aria_snapshot unavailable: {exc})")

        # CDP full AX tree (authoritative) ------------------------------ #
        tree: dict[str, Any] | None = None
        hist: Counter[str] = Counter()
        try:
            cdp = context.new_cdp_session(page)
            cdp.send("Accessibility.enable")
            full = cdp.send("Accessibility.getFullAXTree")
            w("ax_cdp_full.json", full)
            tree, hist = build_cdp_tree(full.get("nodes", []))
            if tree:
                w("ax_cdp_tree.json", tree)
        except Exception as exc:
            print(f"   (CDP AX tree unavailable: {exc})")

        interactive = flatten_interactive(tree) if tree else []
        w("interactive_nodes.json", interactive)
        w("roles_histogram.json", dict(hist.most_common()))

        # DOM control inventory + synthesized labels ------------------- #
        try:
            dom_controls = page.evaluate(DOM_CONTROLS_JS)
            w("dom_controls.json", dom_controls)
        except Exception as exc:
            dom_controls = []
            print(f"   (DOM control inventory failed: {exc})")

        frames = [{"name": f.name, "url": f.url} for f in page.frames]
        w("frames.json", frames)

        # summary ----------------------------------------------------- #
        unnamed_ax = [n for n in interactive if not n["named"]]
        lines = [
            f"URL requested : {args.url}",
            f"URL landed    : {landed_url}",
            f"Title         : {title}",
            f"Frames        : {len(frames)} -> " + ", ".join(f["url"] for f in frames),
            "",
            f"AX tree       : {sum(hist.values())} nodes, {len(interactive)} interactive, "
            f"{len(unnamed_ax)} of those UNNAMED",
            "Top roles     : " + ", ".join(f"{r}={c}" for r, c in hist.most_common(12)),
            "",
            "Interactive AX nodes (role | accessible name | state):",
        ]
        for n in interactive:
            lines.append(f"  {n['role']:<9} | {n['name'][:48]!r:<50} | {n['state'] or ''}")

        lines += ["", "DOM actionable controls (tag/type | ax? | synthesized label | attrs):"]
        for c in dom_controls:
            ident = c["id"] or c["name"] or c["href"][:30] or "-"
            lines.append(
                f"  {c['tag']}/{c['type'] or '-':<7} | "
                f"synth={c['synthesizedLabel'][:40]!r:<42} | id/name/href={ident}"
            )

        if unnamed_ax:
            lines += [
                "",
                "UNNAMED interactive AX nodes — ADR-003's legacy-markup pain point, present",
                "on the real target. perception must synthesise a name (see dom_controls.json",
                "synthesizedLabel: label-for / aria-label / placeholder / nearest text):",
            ]
            for n in unnamed_ax:
                lines.append(
                    f"  {n['role']:<9} @ {n['path']}  "
                    f"(backendDOMNodeId={n['backendDOMNodeId']})"
                )

        summary = "\n".join(lines)
        w("summary.txt", summary)
        print("\n" + summary)
        print(f"\n[OK] wrote {out_dir}/")

        context.close()
        browser.close()


if __name__ == "__main__":
    main()
