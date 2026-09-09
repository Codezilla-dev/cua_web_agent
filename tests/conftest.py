"""Shared fixtures for the pure-function tests."""

from datetime import UTC, datetime

import pytest

from src.types import PerceptionCoverage, UIElement, UIState


def make_element(
    ref: str,
    role: str = "button",
    name: str = "",
    value: str | None = None,
    scope: str = "main",
    enabled: bool = True,
) -> UIElement:
    """A UIElement with only the fields a test cares about."""
    return UIElement(ref=ref, role=role, name=name, value=value, scope=scope, enabled=enabled)


def make_state(
    elements: list[UIElement] | None = None,
    url: str = "http://localhost:5000/member/12345",
    title: str = "Member Record",
) -> UIState:
    """A UIState wrapping the given elements, with coverage stubbed as healthy.

    Coverage is irrelevant to the verifier, so it is fixed here rather than
    recomputed; the coverage gate has its own tests.
    """
    elements = elements or []
    return UIState(
        url=url,
        title=title,
        elements=elements,
        text_digest="digest",
        coverage=PerceptionCoverage(
            ok=True, reason=None, interactive_count=len(elements), unnamed_ratio=0.0
        ),
        captured_at=datetime.now(UTC),
    )


@pytest.fixture
def member_page() -> UIState:
    """The member record page, as perception actually reports it.

    Mirrors a real capture against target_app: a link, and the account table as
    leaf text nodes with the balance in its own element.
    """
    return make_state(
        elements=[
            make_element("e3", "link", "Member Search"),
            make_element("e7", "StaticText", "12345", "12345"),
            make_element("e9", "StaticText", "Dolores Haze", "Dolores Haze"),
            make_element("e17", "StaticText", "Current Balance", "Current Balance"),
            make_element("e18", "StaticText", "SAV-40118", "SAV-40118"),
            make_element("e19", "StaticText", "Savings", "Savings"),
            make_element("e20", "StaticText", "$4,812.55", "$4,812.55"),
            make_element("e22", "StaticText", "Checking", "Checking"),
            make_element("e23", "StaticText", "$1,204.09", "$1,204.09"),
            make_element("e24", "link", "Transfer Funds"),
        ]
    )
