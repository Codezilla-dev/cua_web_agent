"""Tests for the guard stage: `Policy.check`. One test per rule it documents."""

from src.policy.check import Policy
from src.types import ActionKind, Expectation, Intent, RiskTier, TextVisibleCheck
from tests.conftest import make_element, make_state

ALLOWED_ORIGIN = "http://localhost:5000"


def make_policy(
    allowed_origins=(ALLOWED_ORIGIN,),
    allowed_action_types=("navigate", "click", "type", "select", "read", "wait", "done"),
    irreversible_name_pattern=r"(?i)confirm|transfer|delete",
    consequential_actions=("click", "select"),
    safe_actions=("navigate", "type", "read", "wait", "done"),
) -> Policy:
    return Policy(
        allowed_origins=list(allowed_origins),
        allowed_action_types=list(allowed_action_types),
        irreversible_name_pattern=irreversible_name_pattern,
        consequential_actions=list(consequential_actions),
        safe_actions=list(safe_actions),
    )


def make_intent(action, target_ref=None, value=None, risk=RiskTier.SAFE) -> Intent:
    return Intent(
        reasoning="test",
        intent="test step",
        action=action,
        target_ref=target_ref,
        value=value,
        risk=risk,
        expectation=Expectation(description="n/a", check=TextVisibleCheck(text="n/a")),
    )


def test_check_allows_a_safe_action_on_an_allowed_origin_with_a_benign_target():
    state = make_state(
        elements=[make_element("e1", role="button", name="Find Member")],
        url=f"{ALLOWED_ORIGIN}/search",
    )
    intent = make_intent(ActionKind.CLICK, target_ref="e1")

    verdict = make_policy().check(intent, state, target_url=ALLOWED_ORIGIN)

    assert verdict.allowed
    assert verdict.disposition == "allow"
    # click is configured consequential; "Find Member" does not match the
    # irreversible pattern, so policy's own derivation lands here.
    assert verdict.tier == RiskTier.CONSEQUENTIAL


def test_check_halts_when_the_current_page_is_off_origin():
    state = make_state(elements=[], url="https://evil.example.com/phishing")
    intent = make_intent(ActionKind.READ, target_ref="e1")

    verdict = make_policy().check(intent, state, target_url=ALLOWED_ORIGIN)

    assert not verdict.allowed
    assert verdict.disposition == "halt"
    assert verdict.rule == "origin_allowlist"


def test_check_halts_when_a_navigate_targets_an_off_origin_url():
    # The current page is fine; it is where NAVIGATE wants to go that is not.
    state = make_state(elements=[], url=f"{ALLOWED_ORIGIN}/search")
    intent = make_intent(ActionKind.NAVIGATE, value="https://evil.example.com/steal")

    verdict = make_policy().check(intent, state, target_url=ALLOWED_ORIGIN)

    assert not verdict.allowed
    assert verdict.rule == "origin_allowlist"


def test_check_halts_when_the_target_name_matches_the_irreversible_pattern():
    state = make_state(
        elements=[make_element("e5", role="button", name="Transfer Funds")],
        url=f"{ALLOWED_ORIGIN}/account",
    )
    # The planner itself calls this SAFE; policy must not trust that.
    intent = make_intent(ActionKind.CLICK, target_ref="e5", risk=RiskTier.SAFE)

    verdict = make_policy().check(intent, state, target_url=ALLOWED_ORIGIN)

    assert not verdict.allowed
    assert verdict.disposition == "halt"
    assert verdict.rule == "risk.irreversible"
    assert verdict.tier == RiskTier.IRREVERSIBLE


def test_check_allows_typing_into_a_field_merely_named_like_the_irreversible_pattern():
    # The false positive that made this rule action-dependent, with the field
    # named exactly as the live site names it. ParaBank's registration form has
    # a password-confirmation input whose accessible name is the bare string
    # "Confirm:", and `irreversible_name_pattern` contains the word `confirm` to
    # catch a button like "Confirm Transfer". Applying that pattern to any
    # action classified a routine `type` as IRREVERSIBLE and halted the run.
    #
    # Typing into a field commits nothing; only a later click or select can.
    # Verified against https://parabank.parasoft.com/parabank/register.htm on
    # 2026-09-09: perception still reports `textbox` named 'Confirm:' there.
    state = make_state(
        elements=[make_element("e44", role="textbox", name="Confirm:")],
        url=f"{ALLOWED_ORIGIN}/register",
    )
    intent = make_intent(ActionKind.TYPE, target_ref="e44", value="hunter2")

    verdict = make_policy().check(intent, state, target_url=ALLOWED_ORIGIN)

    assert verdict.allowed
    assert verdict.tier == RiskTier.SAFE


def test_check_still_halts_a_click_on_the_same_confirm_named_control():
    # The other half. Narrowing the rule to click/select must not have narrowed
    # it into uselessness: the same name, clicked, is still irreversible.
    state = make_state(
        elements=[make_element("e44", role="button", name="Confirm:")],
        url=f"{ALLOWED_ORIGIN}/register",
    )
    intent = make_intent(ActionKind.CLICK, target_ref="e44")

    verdict = make_policy().check(intent, state, target_url=ALLOWED_ORIGIN)

    assert not verdict.allowed
    assert verdict.tier == RiskTier.IRREVERSIBLE
    assert verdict.rule == "risk.irreversible"


def test_check_halts_when_the_action_type_is_not_allowlisted():
    policy = make_policy(allowed_action_types=("navigate", "read", "wait", "done"))
    state = make_state(
        elements=[make_element("e1", role="button", name="Find Member")],
        url=f"{ALLOWED_ORIGIN}/search",
    )
    intent = make_intent(ActionKind.CLICK, target_ref="e1")

    verdict = policy.check(intent, state, target_url=ALLOWED_ORIGIN)

    assert not verdict.allowed
    assert verdict.rule == "action_allowlist"
