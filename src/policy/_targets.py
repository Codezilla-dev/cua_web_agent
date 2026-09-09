"""Resolve a step's target element by ref.

Shared by `check.py` and `redact.py` so the lookup cannot drift between them.
"""

from src.types import UIElement


def resolve_target_name(target_ref: str | None, elements: list[UIElement]) -> str:
    """Accessible name of the element `target_ref` points at, or `""`.

    `""` means no target or a stale ref; a pattern match against it fails,
    which is right for both.
    """
    if target_ref is None:
        return ""
    for element in elements:
        if element.ref == target_ref:
            return element.name
    return ""
