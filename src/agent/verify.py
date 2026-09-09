"""Did the planner's declared expectation come true? No model consulted.

Checks run against the *perceived elements*, not the page's raw text: the
planner only ever saw the element list, so letting a step pass on text it
never saw would hide perception gaps.

Every branch reports `check_performed` and `evidence` — on failure those two
are the whole debugging story for the step.
"""

import re

from src.types import (
    ElementVisibleCheck,
    Expectation,
    FieldHasValueCheck,
    TextNotVisibleCheck,
    TextVisibleCheck,
    UIElement,
    UIState,
    UrlContainsCheck,
    VerifyResult,
)

# How many observed strings to quote back on failure.
EVIDENCE_SAMPLE_SIZE = 12

# (passed, what was evaluated, what was observed)
CheckOutcome = tuple[bool, str, str]

# Fields whose value we deliberately refuse to observe.
#
# Split into two lists rather than one regex because the short terms are
# ambiguous: "pin" appears inside "Shipping", so it has to match as a whole
# word, while "password" is unambiguous anywhere it appears.
UNOBSERVABLE_SUBSTRINGS = ("password", "passcode", "secret", "token")
UNOBSERVABLE_WORDS = ("pin", "ssn")


def is_unobservable_field(name: str) -> bool:
    """True if `name` refers to a credential whose value we never report."""
    lowered = name.casefold()
    if any(term in lowered for term in UNOBSERVABLE_SUBSTRINGS):
        return True
    words = re.split("[^a-z0-9]+", lowered)
    return any(word in UNOBSERVABLE_WORDS for word in words)


def verify_expectation(expectation: Expectation, state: UIState) -> VerifyResult:
    """Evaluate `expectation` against `state`."""
    check = expectation.check

    if isinstance(check, TextVisibleCheck):
        outcome = _check_text_visible(check, state)
    elif isinstance(check, TextNotVisibleCheck):
        outcome = _check_text_not_visible(check, state)
    elif isinstance(check, ElementVisibleCheck):
        outcome = _check_element_visible(check, state)
    elif isinstance(check, UrlContainsCheck):
        outcome = _check_url_contains(check, state)
    elif isinstance(check, FieldHasValueCheck):
        outcome = _check_field_has_value(check, state)
    else:  # pragma: no cover - the union is closed and pydantic-validated
        outcome = (
            False,
            f"unknown check kind {check!r}",
            "the verifier has no branch for this check; treat as a failure",
        )

    ok, check_performed, evidence = outcome
    return VerifyResult(
        ok=ok,
        expectation=expectation,
        check_performed=check_performed,
        evidence=evidence,
    )


# --------------------------------------------------------------------------- #
# Individual checks
# --------------------------------------------------------------------------- #
def _check_text_visible(check: TextVisibleCheck, state: UIState) -> CheckOutcome:
    matches = _elements_containing(state, check.text, check.case_sensitive)
    performed = (
        f"looked for text {check.text!r} in the perceived elements "
        f"({_sensitivity_note(check.case_sensitive)})"
    )
    if matches:
        found = matches[0]
        return True, performed, f"found in {found.ref} ({found.role}): {_describe(found)!r}"
    return False, performed, _not_found_evidence(state)


def _check_text_not_visible(check: TextNotVisibleCheck, state: UIState) -> CheckOutcome:
    matches = _elements_containing(state, check.text, check.case_sensitive)
    performed = (
        f"confirmed text {check.text!r} is absent from the perceived elements "
        f"({_sensitivity_note(check.case_sensitive)})"
    )
    if not matches:
        return True, performed, f"absent from all {len(state.elements)} perceived elements"
    found = matches[0]
    return (
        False,
        performed,
        f"still present in {found.ref} ({found.role}): {_describe(found)!r}",
    )


def _check_element_visible(check: ElementVisibleCheck, state: UIState) -> CheckOutcome:
    # With neither criterion this matches anything. Fail loudly.
    if check.role is None and check.name_contains is None:
        return (
            False,
            "element_visible with neither role nor name_contains",
            "the check specifies nothing to match, so it cannot be satisfied",
        )

    wanted_name = (check.name_contains or "").casefold()
    matches = [
        element
        for element in state.elements
        if (check.role is None or element.role == check.role)
        and (not wanted_name or wanted_name in element.name.casefold())
    ]

    criteria = []
    if check.role is not None:
        criteria.append(f"role == {check.role!r}")
    if check.name_contains is not None:
        criteria.append(f"name contains {check.name_contains!r}")
    performed = "looked for an element where " + " and ".join(criteria)

    if matches:
        found = matches[0]
        extra = f" (and {len(matches) - 1} more)" if len(matches) > 1 else ""
        return True, performed, f"matched {found.ref}: {found.role} {found.name!r}{extra}"

    # "no button" is easier to act on when you can see it was all links.
    roles_present = sorted({element.role for element in state.elements})
    return (
        False,
        performed,
        f"no element matched; roles present were {roles_present}",
    )


def _check_url_contains(check: UrlContainsCheck, state: UIState) -> CheckOutcome:
    performed = (
        f"looked for {check.fragment!r} in the URL "
        f"({_sensitivity_note(check.case_sensitive)})"
    )
    fragment = check.fragment if check.case_sensitive else check.fragment.casefold()
    url = state.url if check.case_sensitive else state.url.casefold()
    return fragment in url, performed, f"URL was {state.url!r}"


def _check_field_has_value(check: FieldHasValueCheck, state: UIState) -> CheckOutcome:
    # A credential field has no observable post-condition, by design: we refuse
    # to report its value, so we can neither confirm nor deny what it holds.
    #
    # Failing here would be wrong -- it reports a problem with the typing when
    # the only problem is that we chose not to look. It is also expensive: a
    # false failure burns the retry and replan budget, so the next *genuine*
    # mistake dead-ends the run. That is not hypothetical; it is what the traces
    # showed before this branch existed.
    #
    # So the step is accepted on the executor having succeeded, and the trace
    # says plainly that nothing was verified. The real proof lands on the next
    # step, whose expectation is about a page that did or did not change.
    if is_unobservable_field(check.name_contains):
        return (
            True,
            f"skipped: {check.name_contains!r} is a credential field",
            "not verified -- credential values are deliberately never observed, so this "
            "step is accepted on the action succeeding; the next step's expectation is "
            "what actually proves the credentials were taken",
        )

    wanted_name = check.name_contains.casefold()
    candidates = [
        element for element in state.elements if wanted_name in element.name.casefold()
    ]
    performed = (
        f"looked for a field whose name contains {check.name_contains!r} "
        f"and expected its value to be {check.expected!r} "
        f"({_sensitivity_note(check.case_sensitive)})"
    )

    if not candidates:
        names = [element.name for element in state.elements if element.name][
            :EVIDENCE_SAMPLE_SIZE
        ]
        return False, performed, f"no field matched that name; names seen were {names}"

    # Stripped: trailing whitespace is never the difference meant.
    expected = check.expected.strip()
    for element in candidates:
        observed = (element.value or "").strip()
        if _equal(observed, expected, check.case_sensitive):
            return True, performed, f"{element.ref} ({element.name!r}) held {observed!r}"

    observed_values = [
        f"{element.ref}={(element.value or '')!r}" for element in candidates
    ][:EVIDENCE_SAMPLE_SIZE]
    return False, performed, f"matched fields held {observed_values}"


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def _elements_containing(
    state: UIState, needle: str, case_sensitive: bool
) -> list[UIElement]:
    """Elements whose name or value contains `needle`.

    Both are searched: a control's text is its name, a text node's is its value.
    """
    wanted = needle if case_sensitive else needle.casefold()
    found: list[UIElement] = []
    for element in state.elements:
        haystack = f"{element.name} {element.value or ''}"
        if not case_sensitive:
            haystack = haystack.casefold()
        if wanted in haystack:
            found.append(element)
    return found


def _not_found_evidence(state: UIState) -> str:
    """A sample of what the page actually said, for a failed text search."""
    seen = [_describe(element) for element in state.elements if _describe(element)]
    sample = seen[:EVIDENCE_SAMPLE_SIZE]
    suffix = f" (+{len(seen) - len(sample)} more)" if len(seen) > len(sample) else ""
    return f"not found in {len(state.elements)} elements; saw {sample}{suffix}"


def _describe(element: UIElement) -> str:
    """The most informative single string for an element."""
    return element.name or (element.value or "")


def _equal(observed: str, expected: str, case_sensitive: bool) -> bool:
    if case_sensitive:
        return observed == expected
    return observed.casefold() == expected.casefold()


def _sensitivity_note(case_sensitive: bool) -> str:
    return "case-sensitive" if case_sensitive else "case-insensitive"
