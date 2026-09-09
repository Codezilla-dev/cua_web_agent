"""The manual half of a handoff, scripted so it can run unattended.

`cua operator take` moves the lease. It does not click anything -- a real
operator would do that with their hands, in the browser window the run already
opened. This script stands in for the hands so that the whole handoff can be
demonstrated end to end without a person sitting there, which is what makes it
reproducible evidence rather than a screen recording.

What matters is *how* it reaches the page. It attaches to the running browser
over CDP, as a separate process. It does not import the run's surface, does not
hold the run's objects, and could not go through the run's action executor even
if it wanted to -- that executor is exactly what the lease is blocking. This is
the same route a real operator console would take, and it is the reason the
session the human acts on is provably the same session the automation paused.

Usage, against a run paused in a handoff:

    python scripts/operator_hands.py --click "Open Record"

The name is matched against the accessibility name of links and buttons across
every frame, because the control that needs clicking during a drift incident is
usually the one whose name changed.

**Which page it acts on is checked, not assumed.** A CDP endpoint can expose
more than one page -- a stale browser still holding the port, a tab the operator
opened to look something up. Picking the first one would then act on a window
nobody is watching while the run waits on the one they meant, and the handoff
record would show a click that never reached the session. So the inventory is
enumerated: one page is used, several is refused with the list printed and
`--page-url` offered to choose. A console that guesses wrong silently is worse
than one that stops.
"""

import argparse
import sys

from playwright.sync_api import sync_playwright

DEFAULT_CDP = "http://localhost:9222"


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="operator_hands",
        description="Act on the live session a run has handed over.",
    )
    parser.add_argument("--cdp", default=DEFAULT_CDP, help=f"CDP endpoint (default {DEFAULT_CDP}).")
    parser.add_argument("--click", metavar="NAME", help="Click the link or button with this name.")
    parser.add_argument("--fill", nargs=2, metavar=("NAME", "VALUE"), help="Fill a named field.")
    parser.add_argument("--goto", metavar="URL", help="Navigate the session.")
    parser.add_argument(
        "--page-url", metavar="SUBSTRING",
        help="Pick the page whose URL contains this. Needed only when several are open.",
    )
    args = parser.parse_args()

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.connect_over_cdp(args.cdp)
        except Exception as error:
            print(f"operator_hands: cannot reach the session at {args.cdp}: {error}")
            print("  Is a run paused in a handoff, started with --escalate?")
            return 1

        page = _select_page(browser, args.page_url)
        if page is None:
            return 1
        print(f"operator_hands: attached to {page.url}")

        if args.goto:
            page.goto(args.goto)
            print(f"  navigated to {args.goto}")

        if args.fill:
            name, value = args.fill
            if not _fill(page, name, value):
                print(f"  no field named {name!r}")
                return 1
            print(f"  filled {name!r}")

        if args.click:
            if not _click(page, args.click):
                print(f"  no link or button named {args.click!r} in any frame")
                return 1
            print(f"  clicked {args.click!r}")

        page.wait_for_timeout(500)
        print(f"operator_hands: session is now at {page.url}")
        return 0


def _select_page(browser, page_url: str | None):
    """The one page this console may act on, or None having said why.

    The run opens exactly one page, so the common case is unambiguous and needs
    no flag. Ambiguity is the interesting case: it means something else is on
    this endpoint, and any choice made here is a guess about which window the
    operator is looking at. Guessing produces a handoff record that disagrees
    with what happened, which is the one thing this evidence exists to rule out.
    """
    pages = [page for context in browser.contexts for page in context.pages]
    if not pages:
        print("operator_hands: connected, but the session has no page open")
        return None

    if page_url:
        matches = [page for page in pages if page_url in page.url]
        if len(matches) == 1:
            return matches[0]
        print(
            f"operator_hands: --page-url {page_url!r} matches "
            f"{len(matches)} of {len(pages)} pages"
        )
        _print_inventory(pages)
        return None

    if len(pages) == 1:
        return pages[0]

    print(f"operator_hands: {len(pages)} pages are open on this endpoint; refusing to guess")
    _print_inventory(pages)
    print("  Re-run with --page-url SUBSTRING to say which one the run is using.")
    return None


def _print_inventory(pages) -> None:
    for index, page in enumerate(pages):
        print(f"    [{index}] {page.url}")


def _click(page, name: str) -> bool:
    """Click the first matching control, searching every frame.

    Frames matter here specifically: the target app serves its search results
    inside an iframe, which is the legacy-surface property the whole project is
    built around. An operator tool that only looked at the main frame would be
    unable to fix the most likely drift.
    """
    for frame in page.frames:
        for role in ("link", "button"):
            try:
                locator = frame.get_by_role(role, name=name)
                if locator.count():
                    locator.first.click()
                    return True
            except Exception:
                continue
    return False


def _fill(page, name: str, value: str) -> bool:
    for frame in page.frames:
        try:
            locator = frame.get_by_label(name)
            if locator.count():
                locator.first.fill(value)
                return True
        except Exception:
            continue
    return False


if __name__ == "__main__":
    sys.exit(main())
