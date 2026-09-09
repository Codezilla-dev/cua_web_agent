"""Tests for the coverage gate. One failure condition each, plus the combination."""

from src.surface.coverage import compute_coverage
from src.types import PerceptionCoverage, UIElement
from tests.conftest import make_element

MIN_INTERACTIVE = 3
MAX_UNNAMED = 0.4


def compute(
    elements: list[UIElement], empty_frames: list[str] | None = None
) -> PerceptionCoverage:
    return compute_coverage(
        elements=elements,
        empty_frames=empty_frames or [],
        min_interactive_elements=MIN_INTERACTIVE,
        max_unnamed_ratio=MAX_UNNAMED,
    )


def reason_of(coverage: PerceptionCoverage) -> str:
    """The failure reason, asserted present.

    `reason` is `str | None` because a passing gate has nothing to explain.
    Every caller below is inspecting a *failure*, so pinning it here keeps the
    assertions readable and the type checker satisfied.
    """
    assert coverage.reason is not None
    return coverage.reason


def test_healthy_page_passes():
    coverage = compute(
        [
            make_element("e1", name="Sign In"),
            make_element("e2", "textbox", "Operator ID"),
            make_element("e3", "textbox", "Passcode"),
            make_element("e4", "link", "Member Search"),
        ]
    )

    assert coverage.ok
    assert coverage.reason is None
    assert coverage.interactive_count == 4
    assert coverage.unnamed_ratio == 0.0


def test_too_few_interactive_elements_fails():
    coverage = compute([make_element("e1", name="Sign In"), make_element("e2", name="Cancel")])

    assert not coverage.ok
    assert "only 2 interactive elements" in reason_of(coverage)
    assert coverage.interactive_count == 2


def test_no_elements_at_all_fails_without_dividing_by_zero():
    coverage = compute([])

    assert not coverage.ok
    # Reporting 0.0 rather than 1.0 keeps the ratio honest: with nothing found
    # there is no unnamed fraction to speak of, and the count check is the real
    # signal.
    assert coverage.unnamed_ratio == 0.0
    assert "only 0 interactive elements" in reason_of(coverage)


def test_mostly_unnamed_elements_fails():
    # 3 of 5 unnamed = 60% > 40% allowed.
    coverage = compute(
        [
            make_element("e1", name="Sign In"),
            make_element("e2", name="Cancel"),
            make_element("e3", name=""),
            make_element("e4", name=""),
            make_element("e5", name=""),
        ]
    )

    assert not coverage.ok
    assert coverage.unnamed_ratio == 0.6
    assert "3/5 elements have no accessible name" in reason_of(coverage)


def test_whitespace_only_name_counts_as_unnamed():
    # A name of spaces is not a name. Without stripping, a page of blank labels
    # would pass the gate and the planner would get a list it cannot act on.
    coverage = compute(
        [
            make_element("e1", name="Sign In"),
            make_element("e2", name="   "),
            make_element("e3", name="\n\t"),
            make_element("e4", name=""),
        ]
    )

    assert not coverage.ok
    assert coverage.unnamed_ratio == 0.75


def test_unnamed_ratio_exactly_at_threshold_passes():
    # 2 of 5 = 40%, which is not *greater than* 40%. Pinning the boundary so a
    # later refactor cannot silently flip the comparison.
    coverage = compute(
        [
            make_element("e1", name="A"),
            make_element("e2", name="B"),
            make_element("e3", name="C"),
            make_element("e4", name=""),
            make_element("e5", name=""),
        ]
    )

    assert coverage.ok
    assert coverage.unnamed_ratio == 0.4


def test_empty_frame_fails_even_when_the_main_frame_is_healthy():
    # The classic broken-perception signal: the page looks fine, but a frame
    # returned nothing because its tree never loaded.
    coverage = compute(
        [
            make_element("e1", name="Sign In"),
            make_element("e2", "textbox", "Operator ID"),
            make_element("e3", "textbox", "Passcode"),
            make_element("e4", "link", "Member Search"),
        ],
        empty_frames=["frame:directory"],
    )

    assert not coverage.ok
    assert "frame:directory" in reason_of(coverage)
    assert coverage.empty_frames == ["frame:directory"]


def test_reason_reports_every_failure_not_just_the_first():
    coverage = compute(
        [make_element("e1", name=""), make_element("e2", name="")],
        empty_frames=["frame:directory"],
    )

    assert not coverage.ok
    assert "only 2 interactive elements" in reason_of(coverage)
    assert "have no accessible name" in reason_of(coverage)
    assert "frame:directory" in reason_of(coverage)
