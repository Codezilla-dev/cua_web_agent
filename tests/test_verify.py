"""Tests for the expectation verifier.

Every check kind gets a pass and a fail case; the last group pins that a
failure is debuggable and that the verdict depends only on its inputs.
"""

from src.agent.verify import verify_expectation
from src.types import (
    ElementVisibleCheck,
    Expectation,
    FieldHasValueCheck,
    TextNotVisibleCheck,
    TextVisibleCheck,
    UrlContainsCheck,
)
from tests.conftest import make_element, make_state


def expect(check, description="the planner's words"):
    return Expectation(description=description, check=check)


# --------------------------------------------------------------------------- #
# text_visible
# --------------------------------------------------------------------------- #
def test_text_visible_passes_when_text_is_on_the_page(member_page):
    result = verify_expectation(expect(TextVisibleCheck(text="Dolores Haze")), member_page)

    assert result.ok
    assert "e9" in result.evidence


def test_text_visible_fails_and_reports_what_was_seen(member_page):
    result = verify_expectation(expect(TextVisibleCheck(text="Marcus Whitfield")), member_page)

    assert not result.ok
    # The failure has to be debuggable from the record alone: what was looked
    # for, and what the page actually said.
    assert "Marcus Whitfield" in result.check_performed
    assert "Dolores Haze" in result.evidence


def test_text_visible_is_case_insensitive_by_default(member_page):
    assert verify_expectation(expect(TextVisibleCheck(text="dolores haze")), member_page).ok


def test_text_visible_honours_case_sensitive_flag(member_page):
    result = verify_expectation(
        expect(TextVisibleCheck(text="dolores haze", case_sensitive=True)), member_page
    )

    assert not result.ok
    assert "case-sensitive" in result.check_performed


def test_text_visible_matches_a_substring_of_an_element(member_page):
    # "$4,812.55" lives in one element; asking for part of it should still match.
    assert verify_expectation(expect(TextVisibleCheck(text="4,812")), member_page).ok


def test_text_visible_searches_names_as_well_as_values():
    # A control's text is its name; a readable node carries it in value. The
    # planner does not distinguish, so neither can the verifier.
    state = make_state([make_element("e1", "button", name="Confirm Transfer", value=None)])

    assert verify_expectation(expect(TextVisibleCheck(text="Confirm Transfer")), state).ok


# --------------------------------------------------------------------------- #
# text_not_visible
# --------------------------------------------------------------------------- #
def test_text_not_visible_passes_when_absent(member_page):
    result = verify_expectation(
        expect(TextNotVisibleCheck(text="Record Not Found")), member_page
    )

    assert result.ok


def test_text_not_visible_fails_and_names_the_offending_element(member_page):
    result = verify_expectation(expect(TextNotVisibleCheck(text="Savings")), member_page)

    assert not result.ok
    assert "e19" in result.evidence


# --------------------------------------------------------------------------- #
# element_visible
# --------------------------------------------------------------------------- #
def test_element_visible_matches_on_role_alone(member_page):
    assert verify_expectation(expect(ElementVisibleCheck(role="link")), member_page).ok


def test_element_visible_matches_on_role_and_name(member_page):
    result = verify_expectation(
        expect(ElementVisibleCheck(role="link", name_contains="Transfer")), member_page
    )

    assert result.ok
    assert "e24" in result.evidence


def test_element_visible_fails_when_role_is_absent_and_lists_roles_present(member_page):
    result = verify_expectation(expect(ElementVisibleCheck(role="textbox")), member_page)

    assert not result.ok
    # Knowing the page only had links and text is what tells you the click never
    # landed, rather than that the name was wrong.
    assert "link" in result.evidence
    assert "StaticText" in result.evidence


def test_element_visible_fails_when_role_matches_but_name_does_not(member_page):
    result = verify_expectation(
        expect(ElementVisibleCheck(role="link", name_contains="Sign Out")), member_page
    )

    assert not result.ok


def test_element_visible_with_no_criteria_fails_rather_than_matching_anything(member_page):
    # An empty check would otherwise match the first element on any page and
    # report success for a step that did nothing.
    result = verify_expectation(expect(ElementVisibleCheck()), member_page)

    assert not result.ok
    assert "cannot be satisfied" in result.evidence


# --------------------------------------------------------------------------- #
# url_contains
# --------------------------------------------------------------------------- #
def test_url_contains_passes(member_page):
    result = verify_expectation(expect(UrlContainsCheck(fragment="/member/12345")), member_page)

    assert result.ok
    assert "/member/12345" in result.evidence


def test_url_contains_fails_and_reports_the_actual_url(member_page):
    result = verify_expectation(expect(UrlContainsCheck(fragment="/transfer")), member_page)

    assert not result.ok
    assert "http://localhost:5000/member/12345" in result.evidence


def test_url_contains_ignores_case(member_page):
    assert verify_expectation(expect(UrlContainsCheck(fragment="/MEMBER/")), member_page).ok


# --------------------------------------------------------------------------- #
# field_has_value
# --------------------------------------------------------------------------- #
def test_field_has_value_passes_when_the_typed_value_landed():
    state = make_state([make_element("e9", "textbox", name="Member ID or Name", value="12345")])
    result = verify_expectation(
        expect(FieldHasValueCheck(name_contains="Member ID", expected="12345")), state
    )

    assert result.ok
    assert "e9" in result.evidence


def test_field_has_value_fails_when_the_field_is_empty():
    # The exact symptom of a fill() that silently did not take.
    state = make_state([make_element("e9", "textbox", name="Member ID or Name", value=None)])
    result = verify_expectation(
        expect(FieldHasValueCheck(name_contains="Member ID", expected="12345")), state
    )

    assert not result.ok
    assert "e9" in result.evidence


def test_field_has_value_fails_when_no_field_matches_the_name_and_lists_names():
    state = make_state([make_element("e1", "textbox", name="Passcode", value="x")])
    result = verify_expectation(
        expect(FieldHasValueCheck(name_contains="Member ID", expected="12345")), state
    )

    assert not result.ok
    assert "Passcode" in result.evidence


def test_field_has_value_ignores_surrounding_whitespace():
    state = make_state([make_element("e9", "textbox", name="Amount", value="  25.00  ")])

    assert verify_expectation(
        expect(FieldHasValueCheck(name_contains="Amount", expected="25.00")), state
    ).ok


def test_field_has_value_checks_every_candidate_not_just_the_first():
    # Two fields whose names both contain "Account"; only the second holds the
    # value. Stopping at the first would be a false negative.
    state = make_state(
        [
            make_element("e1", "textbox", name="From Account", value="SAV-40118"),
            make_element("e2", "textbox", name="To Account", value="EXT-99"),
        ]
    )

    assert verify_expectation(
        expect(FieldHasValueCheck(name_contains="Account", expected="EXT-99")), state
    ).ok


# --------------------------------------------------------------------------- #
# Properties the loop relies on
# --------------------------------------------------------------------------- #
def test_result_carries_the_original_expectation_for_the_trace(member_page):
    expectation = expect(TextVisibleCheck(text="Savings"), description="the balance is shown")
    result = verify_expectation(expectation, member_page)

    assert result.expectation is expectation
    assert result.expectation.description == "the balance is shown"


def test_description_is_never_evaluated_only_the_check(member_page):
    # The planner's prose says one thing, the check says another. The verdict
    # must follow the check: prose is not evidence.
    expectation = expect(
        TextVisibleCheck(text="Marcus Whitfield"),
        description="Dolores Haze is clearly visible on the page",
    )

    assert not verify_expectation(expectation, member_page).ok


def test_verification_is_repeatable(member_page):
    expectation = expect(TextVisibleCheck(text="Savings"))
    first = verify_expectation(expectation, member_page)
    second = verify_expectation(expectation, member_page)

    assert (first.ok, first.check_performed, first.evidence) == (
        second.ok,
        second.check_performed,
        second.evidence,
    )


def test_empty_page_fails_every_positive_check():
    empty = make_state([])

    assert not verify_expectation(expect(TextVisibleCheck(text="anything")), empty).ok
    assert not verify_expectation(expect(ElementVisibleCheck(role="button")), empty).ok
    assert not verify_expectation(
        expect(FieldHasValueCheck(name_contains="x", expected="y")), empty
    ).ok
    # ...but a negative check is legitimately satisfied by an empty page.
    assert verify_expectation(expect(TextNotVisibleCheck(text="anything")), empty).ok


# --------------------------------------------------------------------------- #
# Credential fields are not observable
#
# We refuse to report a password's value, so a `field_has_value` naming one asks
# a question the system has chosen not to answer. Failing it would blame the
# typing for a decision we made about looking -- and would burn the retry budget
# that a genuine later mistake needs.
# --------------------------------------------------------------------------- #
def test_field_has_value_on_a_credential_field_is_not_treated_as_a_failure():
    # Exactly what the browser reports for a filled password input.
    state = make_state([make_element("e10", "textbox", name="Passcode", value="\u2022" * 8)])

    result = verify_expectation(
        expect(FieldHasValueCheck(name_contains="Passcode", expected="hunter2")), state
    )

    assert result.ok
    assert "credential" in result.check_performed
    # The trace must say plainly that nothing was checked, so nobody reads this
    # as evidence the value was confirmed.
    assert "not verified" in result.evidence


def test_credential_rule_covers_the_usual_field_names():
    for field_name in ("Password", "passcode", "Account PIN", "SSN", "API token"):
        state = make_state([make_element("e1", "textbox", name=field_name, value="x")])
        result = verify_expectation(
            expect(FieldHasValueCheck(name_contains=field_name, expected="anything")), state
        )
        assert result.ok, f"{field_name} should be treated as unobservable"


def test_ordinary_fields_are_still_verified_normally():
    # The rule must not become a blanket excuse: a non-credential field that
    # genuinely did not take the value still fails.
    state = make_state([make_element("e9", "textbox", name="Member ID", value="")])

    result = verify_expectation(
        expect(FieldHasValueCheck(name_contains="Member ID", expected="12345")), state
    )

    assert not result.ok


def test_pin_matches_as_a_word_not_inside_another_word():
    # "Shipping" contains "pin"; it is not a credential field.
    state = make_state([make_element("e1", "textbox", name="Shipping Code", value="abc")])

    result = verify_expectation(
        expect(FieldHasValueCheck(name_contains="Shipping Code", expected="xyz")), state
    )

    assert not result.ok


def test_url_contains_honours_case_sensitive_when_asked(member_page):
    # Default stays insensitive; asking for exactness is respected.
    assert verify_expectation(expect(UrlContainsCheck(fragment="/MEMBER/")), member_page).ok
    assert not verify_expectation(
        expect(UrlContainsCheck(fragment="/MEMBER/", case_sensitive=True)), member_page
    ).ok
