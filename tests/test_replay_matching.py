"""How replay finds the control a step describes.

`AnchorSpec` is a headline claim -- the answer to a `read` target whose
accessible name was the recorded value itself (`$4,812.55`, which is not *where*
the balance is, it is what it *was*). It had no test. Neither did `match_index`,
which is the difference between "a link called Open Record" and "the second link
called Open Record".

These are the two places where a wrong match is worse than no match: reading a
confidently wrong value beats failing to read one, from the caller's point of
view, right up until they act on it.

The chain itself is the other subject here. Each rung gives up something, so
what matters is that the order is respected and that role and scope are never
relaxed -- and that the rung which fired is reported, because a capability that
still resolves only because of `contains` is drifting.
"""

from src.artifact.models import AnchorSpec, CapabilityStep, TargetSpec
from src.replay.engine import _match_by_anchor, _match_element
from src.types import ActionKind, Expectation, RiskTier, TextVisibleCheck
from tests.fake_surface import element, page


def step_for(target: TargetSpec) -> CapabilityStep:
    return CapabilityStep(
        index=0,
        intent="find it",
        action=ActionKind.CLICK,
        target=target,
        risk=RiskTier.SAFE,
        expectation=Expectation(
            description="whatever", check=TextVisibleCheck(kind="text_visible", text="x")
        ),
    )


def spec(
    role: str = "link",
    name: str = "Open Record",
    scope: str = "main",
    match_index: int = 0,
    anchor: AnchorSpec | None = None,
) -> TargetSpec:
    return TargetSpec(
        role=role, name=name, scope=scope, match_index=match_index, anchor=anchor
    )


# --- role, name and scope ---------------------------------------------------
def test_a_control_is_matched_on_role_name_and_scope_together():
    state = page(
        "u",
        [
            element("e1", "StaticText", "Open Record"),   # right name, wrong role
            element("e2", "link", "Open Record", scope="frame:directory"),  # wrong scope
            element("e3", "link", "Open Record"),         # the one
        ],
    )

    matched = _match_element(state, step_for(spec()))

    assert matched is not None
    assert matched.element.ref == "e3"
    assert matched.strategy == "exact"


def test_a_control_that_is_no_longer_there_matches_nothing():
    state = page("u", [element("e1", "link", "Something Else")])

    assert _match_element(state, step_for(spec())) is None


def test_the_scope_of_the_recording_is_honoured():
    # The target app serves its search results in an iframe, so "which frame"
    # is load-bearing rather than incidental.
    state = page(
        "u",
        [
            element("e1", "link", "Open Record"),
            element("e2", "link", "Open Record", scope="frame:directory"),
        ],
    )

    matched = _match_element(state, step_for(spec(scope="frame:directory")))

    assert matched is not None
    assert matched.element.ref == "e2"


# --- match_index ------------------------------------------------------------
def test_match_index_picks_the_occurrence_the_recording_used():
    state = page(
        "u",
        [
            element("e1", "link", "Open Record"),
            element("e2", "link", "Open Record"),
            element("e3", "link", "Open Record"),
        ],
    )

    first = _match_element(state, step_for(spec(match_index=0)))
    third = _match_element(state, step_for(spec(match_index=2)))

    assert first is not None and third is not None
    assert (first.element.ref, third.element.ref) == ("e1", "e3")


def test_fewer_matches_than_the_recording_saw_is_a_failure_not_a_guess():
    # A page with three identically-named links that now has two is a real
    # change. Falling back to the first would click something else confidently.
    state = page(
        "u",
        [element("e1", "link", "Open Record"), element("e2", "link", "Open Record")],
    )

    assert _match_element(state, step_for(spec(match_index=2))) is None


# --- anchors ----------------------------------------------------------------
def test_an_anchor_finds_the_value_sitting_past_a_stable_label():
    # The balance's own name is the recorded balance, so it cannot be matched by
    # name for any other member. "Two past the cell that says Savings" can.
    state = page(
        "u",
        [
            element("e1", "StaticText", "SAV-51002"),
            element("e2", "StaticText", "Savings"),
            element("e3", "StaticText", "$918.40", "$918.40"),
        ],
    )
    anchored = spec(
        role="StaticText",
        name="$4,812.55",  # what the recording saw; wrong for this member
        anchor=AnchorSpec(after_name="Savings", offset=1, role="StaticText"),
    )

    matched = _match_element(state, step_for(anchored))

    assert matched is not None
    assert matched.element.ref == "e3"
    assert matched.element.value == "$918.40"
    assert matched.strategy == "anchor"


def test_the_anchor_is_preferred_over_the_recorded_name_even_when_that_name_exists():
    # The name is the thing known to be wrong. Trying it first would find the
    # right element only for the input it was recorded with.
    state = page(
        "u",
        [
            element("e1", "StaticText", "Savings"),
            element("e2", "StaticText", "$918.40", "$918.40"),
            element("e9", "StaticText", "$4,812.55", "$4,812.55"),
        ],
    )
    anchored = spec(
        role="StaticText",
        name="$4,812.55",
        anchor=AnchorSpec(after_name="Savings", offset=1, role="StaticText"),
    )

    matched = _match_element(state, step_for(anchored))

    assert matched is not None
    assert matched.element.ref == "e2"
    assert matched.strategy == "anchor"


def test_an_anchor_landing_on_the_wrong_role_resolves_nothing():
    # If the page grew a row and the offset now points at something else,
    # failing is right: a wrong value read confidently is worse than an error.
    state = page(
        "u",
        [
            element("e1", "StaticText", "Savings"),
            element("e2", "link", "Transfer Funds"),
        ],
    )

    assert _match_by_anchor(
        state, spec(anchor=AnchorSpec(after_name="Savings", offset=1, role="StaticText"))
    ) is None


def test_an_anchor_whose_label_is_gone_resolves_nothing():
    state = page("u", [element("e1", "StaticText", "Chequing")])

    assert _match_by_anchor(
        state, spec(anchor=AnchorSpec(after_name="Savings", offset=1, role="StaticText"))
    ) is None


def test_an_anchor_pointing_past_the_end_of_the_page_resolves_nothing():
    state = page("u", [element("e1", "StaticText", "Savings")])

    assert _match_by_anchor(
        state, spec(anchor=AnchorSpec(after_name="Savings", offset=5, role="StaticText"))
    ) is None


def test_an_anchor_counts_positions_within_its_own_scope():
    # Offsets are meaningless across frames: the main document's element list
    # and an iframe's are two different sequences.
    state = page(
        "u",
        [
            element("e1", "StaticText", "Savings"),
            element("e2", "StaticText", "main-frame-value", "main"),
            element("e3", "StaticText", "Savings", scope="frame:directory"),
            element("e4", "StaticText", "framed-value", "framed", scope="frame:directory"),
        ],
    )

    matched = _match_by_anchor(
        state,
        spec(
            scope="frame:directory",
            anchor=AnchorSpec(after_name="Savings", offset=1, role="StaticText"),
        ),
    )

    assert matched is not None
    assert matched.ref == "e4"


def test_falling_back_to_the_name_when_the_anchor_does_not_resolve():
    # An anchor that misses is not fatal on its own; the recorded name is still
    # worth trying, and for the recorded input it will work.
    state = page("u", [element("e1", "StaticText", "$4,812.55", "$4,812.55")])
    anchored = spec(
        role="StaticText",
        name="$4,812.55",
        anchor=AnchorSpec(after_name="Savings", offset=1, role="StaticText"),
    )

    matched = _match_element(state, step_for(anchored))

    assert matched is not None
    assert matched.element.ref == "e1"
    # The anchor missed, so the recorded name carried it -- and the trace says
    # so rather than reporting an unqualified success.
    assert matched.strategy == "exact"


# --- the ranked chain -------------------------------------------------------
def test_a_name_that_differs_only_in_case_and_whitespace_still_resolves():
    # The two differences that are almost never meaningful and are the most
    # common way a rendered name shifts between releases.
    state = page("u", [element("e1", "link", "  OPEN   Record ")])

    matched = _match_element(state, step_for(spec(name="Open Record")))

    assert matched is not None
    assert matched.element.ref == "e1"
    assert matched.strategy == "normalised"


def test_a_name_the_vendor_has_added_words_to_still_resolves_as_contains():
    # The drift this project is built around: a vendor relabels a control and
    # the recorded name survives inside the new one.
    state = page("u", [element("e1", "link", "Open Record (renamed by the vendor)")])

    matched = _match_element(state, step_for(spec(name="Open Record")))

    assert matched is not None
    assert matched.element.ref == "e1"
    assert matched.strategy == "contains"


def test_an_exact_match_wins_over_a_looser_one_elsewhere_on_the_page():
    # Order is the whole design. A page holding both must not resolve to the
    # loose one just because it comes first in the element list.
    state = page(
        "u",
        [
            element("e1", "link", "Open Record (renamed by the vendor)"),
            element("e2", "link", "Open Record"),
        ],
    )

    matched = _match_element(state, step_for(spec(name="Open Record")))

    assert matched is not None
    assert matched.element.ref == "e2"
    assert matched.strategy == "exact"


def test_a_normalised_match_wins_over_a_contains_match():
    state = page(
        "u",
        [
            element("e1", "link", "Open Record and more besides"),
            element("e2", "link", "open   record"),
        ],
    )

    matched = _match_element(state, step_for(spec(name="Open Record")))

    assert matched is not None
    assert matched.element.ref == "e2"
    assert matched.strategy == "normalised"


def test_the_chain_never_relaxes_the_role():
    # A link is not a button. Loosening this would not be tolerating drift, it
    # would be resolving a different control.
    state = page("u", [element("e1", "button", "Open Record")])

    assert _match_element(state, step_for(spec(role="link"))) is None


def test_the_chain_never_relaxes_the_frame():
    # The main document is not an iframe, for the same reason.
    state = page("u", [element("e1", "link", "Open Record", scope="frame:directory")])

    assert _match_element(state, step_for(spec(scope="main"))) is None


def test_an_empty_recorded_name_does_not_match_everything():
    # `contains` on the empty string is true of every element, which would turn
    # the last rung into "any control of this role", silently.
    state = page("u", [element("e1", "link", "Something Else")])

    assert _match_element(state, step_for(spec(name=""))) is None


def test_match_index_applies_within_the_strategy_that_fired():
    state = page(
        "u",
        [
            element("e1", "link", "Open Record (renamed)"),
            element("e2", "link", "Open Record (also renamed)"),
        ],
    )

    matched = _match_element(state, step_for(spec(name="Open Record", match_index=1)))

    assert matched is not None
    assert matched.element.ref == "e2"
    assert matched.strategy == "contains"
