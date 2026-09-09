"""Tests for the production path: what replay returns, and why.

`src/replay/engine.py` decides whether a run is a success, a definitive answer
from the application, or a failure -- and the taxonomy it returns is a headline
claim of this project. None of it was tested, on the grounds that replay needs a
browser. It does not: `ReplayRequest` takes the surface as an argument, so the
whole engine can be driven from a scripted list of pages.

Each test here corresponds to a claim made elsewhere in the documentation. The
one that matters most is the wrong-record test: the capability's checkpoint has
to be able to tell "we fetched member 22841" from "we fetched some member".
"""

from datetime import UTC, datetime

import pytest

from src.artifact.models import (
    Capability,
    CapabilityStep,
    Constant,
    CredentialRef,
    OutputSpec,
    ParamRef,
    ParamSpec,
    Provenance,
    SurfaceRef,
    TargetSpec,
)
from src.escalation.conditions import StuckCondition
from src.replay.engine import ReplayRequest, replay
from src.replay.escalate import EscalationPolicy
from src.replay.outcomes import (
    BusinessOutcome,
    BusinessOutcomeRule,
    FailureKind,
    HardFailure,
    Success,
)
from src.types import (
    ActionKind,
    Expectation,
    RiskTier,
    TextVisibleCheck,
    UrlContainsCheck,
)
from tests.fake_surface import FakeSurface, element, page, permissive_policy

SEARCH_URL = "http://localhost:5000/search"
RECORD_URL = "http://localhost:5000/member/22841"

BUSINESS_RULES = [
    BusinessOutcomeRule(code="RECORD_NOT_FOUND", pattern="(?i)no member records match"),
]


def capability(
    steps: list[CapabilityStep] | None = None,
    inputs: list[ParamSpec] | None = None,
    outputs: list[OutputSpec] | None = None,
    checkpoint: Expectation | None = None,
) -> Capability:
    return Capability(
        id="look_up_member_read",
        title="look up member {{member_id_or_name}} and read their savings balance",
        surface=SurfaceRef(
            kind="web", entry_path="/search", recorded_origin="http://localhost:5000"
        ),
        inputs=inputs if inputs is not None else [
            ParamSpec(name="member_id_or_name", example="12345")
        ],
        outputs=outputs if outputs is not None else [
            OutputSpec(name="savings_account_balance", description="balance", source_step=1)
        ],
        steps=steps if steps is not None else default_steps(),
        checkpoint=checkpoint or Expectation(
            description="The URL identifies the requested record.",
            check=UrlContainsCheck(kind="url_contains", fragment="{{member_id_or_name}}"),
        ),
        provenance=Provenance(
            discovery_run_id="20260909T000000Z-test",
            goal="look up member 12345 and read their savings balance",
            model="test",
            recorded_at=datetime.now(UTC),
            source_step_count=2,
            retried_step_count=0,
        ),
    )


def default_steps() -> list[CapabilityStep]:
    """Type the member id, then read the balance. The shortest flow that has
    a parameter, an output and a checkpoint worth asserting."""
    return [
        CapabilityStep(
            index=0,
            intent="type the member id",
            action=ActionKind.TYPE,
            target=TargetSpec(
                role="textbox", name="Member ID or Name", scope="main", match_index=0
            ),
            value=ParamRef(kind="param", name="member_id_or_name"),
            risk=RiskTier.SAFE,
            expectation=Expectation(
                description="still on search",
                check=UrlContainsCheck(kind="url_contains", fragment="/search"),
            ),
        ),
        CapabilityStep(
            index=1,
            intent="read the savings balance",
            action=ActionKind.READ,
            target=TargetSpec(role="StaticText", name="Balance", scope="main", match_index=0),
            risk=RiskTier.SAFE,
            expectation=Expectation(
                description="balance visible",
                check=TextVisibleCheck(kind="text_visible", text="$918.40"),
            ),
        ),
    ]


def run(
    surface: FakeSurface,
    cap: Capability | None = None,
    params: dict[str, str] | None = None,
    policy=None,
):
    request = ReplayRequest(
        capability=cap or capability(),
        params=params if params is not None else {"member_id_or_name": "22841"},
        surface=surface,
        policy=policy or permissive_policy(),
        credentials={"username": "operator", "password": "secret"},
        business_rules=BUSINESS_RULES,
        settle_timeout_s=0.0,
        evidence_dir="evidence/test",
    )
    return replay(request)


def search_page(url: str = SEARCH_URL):
    return page(url, [element("e1", "textbox", "Member ID or Name")])


def record_page(url: str = RECORD_URL, balance: str = "$918.40"):
    return page(
        url,
        [element("e5", "StaticText", "Balance", balance)],
        text_digest=f"Savings {balance}",
    )


# --- the happy path ---------------------------------------------------------
def test_a_replay_that_reaches_the_requested_record_succeeds_with_its_outputs():
    surface = FakeSurface([search_page(), search_page(), record_page(), record_page()])

    result = run(surface)

    assert isinstance(result, Success)
    assert result.outputs == {"savings_account_balance": "$918.40"}
    assert result.steps_run == 2


def test_the_surface_is_always_stopped_even_when_the_run_fails():
    surface = FakeSurface([search_page(), search_page(), record_page(), record_page()])

    run(surface)

    assert surface.started and surface.stopped


def test_the_caller_supplied_parameter_is_what_gets_typed():
    # Not the recorded example. This is the difference between a capability and
    # a macro, so it is worth asserting on the action rather than the outcome.
    surface = FakeSurface([search_page(), search_page(), record_page(), record_page()])

    run(surface, params={"member_id_or_name": "22841"})

    typed = [i for i in surface.performed if i.action is ActionKind.TYPE]
    assert [i.value for i in typed] == ["22841"]


# --- the checkpoint ---------------------------------------------------------
def test_arriving_at_the_wrong_record_is_a_checkpoint_failure_not_a_success():
    # The regression this whole fix exists for. Every step runs, a balance is
    # read, and it is the wrong member's. Before the checkpoint bound its
    # parameter this returned success with member 12345's money.
    wrong = record_page("http://localhost:5000/member/12345", "$4,812.55")
    surface = FakeSurface([search_page(), search_page(), wrong, wrong])

    result = run(surface, params={"member_id_or_name": "22841"})

    assert isinstance(result, HardFailure)
    assert result.kind is FailureKind.CHECKPOINT_FAILED
    assert "22841" in result.expected
    assert "12345" in result.observed


def test_the_checkpoint_binds_the_caller_parameter_not_the_recorded_example():
    # Landing on the *recorded* record while a different one was requested is
    # the same failure. Asserting this separately stops a checkpoint that
    # accidentally hardcodes the example from passing the test above.
    recorded = record_page("http://localhost:5000/member/12345", "$4,812.55")
    surface = FakeSurface([search_page(), search_page(), recorded, recorded])

    result = run(surface, params={"member_id_or_name": "12345"})

    assert isinstance(result, Success)


# --- the application answering definitively ---------------------------------
def test_a_record_that_does_not_exist_is_a_business_outcome_not_a_failure():
    # "No member 99999 exists" is a correct answer the caller needs. Treating it
    # as an error is how automation retries a lookup that can never succeed.
    empty = page(
        SEARCH_URL,
        [
            element("e1", "textbox", "Member ID or Name"),
            element("e2", "StaticText", "No member records match that search."),
        ],
    )
    surface = FakeSurface([search_page(), empty])

    result = run(surface, params={"member_id_or_name": "99999"})

    assert isinstance(result, BusinessOutcome)
    assert result.code == "RECORD_NOT_FOUND"


def test_a_business_outcome_outranks_the_steps_own_expectation():
    # The step's expectation would also have failed. Reporting that would
    # describe the symptom; the application's answer is the cause.
    empty = page(
        "http://localhost:5000/nowhere",
        [element("e2", "StaticText", "No member records match that search.")],
    )
    surface = FakeSurface([search_page(), empty])

    result = run(surface, params={"member_id_or_name": "99999"})

    assert isinstance(result, BusinessOutcome)


# --- bad requests -----------------------------------------------------------
def test_a_missing_required_parameter_is_rejected_before_the_browser_opens():
    surface = FakeSurface([search_page()])

    result = run(surface, params={})

    assert isinstance(result, HardFailure)
    assert result.kind is FailureKind.INVALID_REQUEST
    assert "member_id_or_name" in result.expected
    assert not surface.started, "an invalid request must not launch a surface"


def test_an_empty_string_does_not_count_as_a_supplied_parameter():
    surface = FakeSurface([search_page()])

    result = run(surface, params={"member_id_or_name": ""})

    assert isinstance(result, HardFailure)
    assert result.kind is FailureKind.INVALID_REQUEST


# --- policy -----------------------------------------------------------------
def test_policy_halts_replay_exactly_as_it_halts_discovery():
    # Replay is not exempt from policy. A capability recorded before a rule
    # existed must not be able to outrun it. This drives the real `Policy`
    # with `type` withheld from the action allowlist, so the halt comes from
    # the same code the discovery loop's guard stage runs.
    surface = FakeSurface([search_page(), search_page(), record_page(), record_page()])
    restricted = permissive_policy(
        allowed_action_types=["navigate", "click", "read", "wait", "done"]
    )

    result = run(surface, policy=restricted)

    assert isinstance(result, HardFailure)
    assert result.kind is FailureKind.POLICY_HALTED
    assert "action_allowlist" in result.observed


def test_an_irreversible_target_halts_replay_on_the_risk_rule():
    # The other halt, and the one that matters: a control whose name says it
    # commits something. The tier is derived by policy from the observed
    # element, not read off the capability.
    steps = default_steps()
    steps[0] = steps[0].model_copy(
        update={
            "action": ActionKind.CLICK,
            "value": None,
            "target": TargetSpec(
                role="button", name="Confirm Transfer", scope="main", match_index=0
            ),
        }
    )
    commit = page(SEARCH_URL, [element("e1", "button", "Confirm Transfer")])
    surface = FakeSurface([commit, commit, record_page(), record_page()])

    result = run(surface, cap=capability(steps=steps))

    assert isinstance(result, HardFailure)
    assert result.kind is FailureKind.POLICY_HALTED
    assert "risk.irreversible" in result.observed


def test_a_halted_action_is_never_performed():
    surface = FakeSurface([search_page(), search_page(), record_page(), record_page()])

    run(
        surface,
        policy=permissive_policy(
            allowed_action_types=["navigate", "click", "read", "wait", "done"]
        ),
    )

    assert surface.performed == [], "the halted action reached the executor anyway"


# --- targets and failures ---------------------------------------------------
def test_a_target_that_is_no_longer_on_the_page_fails_with_what_it_looked_for():
    bare = page(SEARCH_URL, [element("e9", "link", "Something Else")])
    surface = FakeSurface([bare])

    result = run(surface)

    assert isinstance(result, HardFailure)
    assert result.kind is FailureKind.TARGET_NOT_FOUND
    assert "Member ID or Name" in result.expected


def test_an_action_the_surface_could_not_perform_is_reported_as_action_failed():
    surface = FakeSurface(
        [search_page(), search_page(), record_page(), record_page()],
        failing_actions={ActionKind.TYPE: "element was detached"},
    )

    result = run(surface)

    assert isinstance(result, HardFailure)
    assert result.kind is FailureKind.ACTION_FAILED
    assert "detached" in result.observed


def test_a_step_whose_expectation_does_not_hold_is_reported_against_that_step():
    elsewhere = page("http://localhost:5000/elsewhere",
                     [element("e1", "textbox", "Member ID or Name")])
    surface = FakeSurface([search_page(), elsewhere])

    result = run(surface)

    assert isinstance(result, HardFailure)
    assert result.kind is FailureKind.EXPECTATION_FAILED
    assert result.step_index == 0


def test_a_surface_that_will_not_start_is_a_surface_error():
    surface = FakeSurface([search_page()], fail_on_start="no browser available")

    result = run(surface)

    assert isinstance(result, HardFailure)
    assert result.kind is FailureKind.SURFACE_ERROR
    assert "no browser" in result.observed


# --- reads ------------------------------------------------------------------
def test_a_read_that_returns_nothing_fails_rather_than_reporting_an_empty_output():
    blank = page(RECORD_URL, [element("e5", "StaticText", "Balance", "")],
                 text_digest="Savings")
    surface = FakeSurface([search_page(), search_page(), blank, blank])

    result = run(surface)

    assert isinstance(result, HardFailure)
    assert result.kind is FailureKind.EXPECTATION_FAILED
    assert result.step_index == 1


def test_a_read_is_judged_on_returning_something_not_on_the_recorded_value():
    # The recorded expectation quotes $918.40. A different member's balance is
    # the correct answer for a different input, and must not look like failure.
    other = record_page(RECORD_URL, "$77.00")
    surface = FakeSurface([search_page(), search_page(), other, other])

    result = run(surface)

    assert isinstance(result, Success)
    assert result.outputs == {"savings_account_balance": "$77.00"}


# --- constants and credentials ----------------------------------------------
def test_a_constant_value_is_typed_verbatim():
    steps = default_steps()
    steps[0] = steps[0].model_copy(
        update={"value": Constant(kind="constant", value="fixed-value")}
    )
    surface = FakeSurface([search_page(), search_page(), record_page(), record_page()])

    run(surface, cap=capability(steps=steps, inputs=[]), params={})

    typed = [i for i in surface.performed if i.action is ActionKind.TYPE]
    assert [i.value for i in typed] == ["fixed-value"]


@pytest.mark.parametrize("supplied", ["22841", "12345"])
def test_the_same_capability_serves_records_it_was_never_recorded_with(supplied):
    landed = record_page(f"http://localhost:5000/member/{supplied}", "$1.00")
    surface = FakeSurface([search_page(), search_page(), landed, landed])

    result = run(surface, params={"member_id_or_name": supplied})

    assert isinstance(result, Success)


# --- what only shows across steps -------------------------------------------
#
# Two stuck conditions the brief names cannot be seen from inside one step,
# because at each individual step nothing has gone wrong. Both were declared in
# the enum long before anything could raise them; these are the tests that say
# they are reachable.
def stalled_capability() -> Capability:
    """Three clicks whose expectations are loose enough to hold on a page that
    never changes -- the shape a run has when it is going nowhere and still
    reporting success at every step."""

    def click(index: int, expectation: Expectation) -> CapabilityStep:
        return CapabilityStep(
            index=index,
            intent=f"click {index}",
            action=ActionKind.CLICK,
            target=TargetSpec(role="button", name="Find Member", scope="main", match_index=0),
            risk=RiskTier.SAFE,
            expectation=expectation,
        )

    loose = Expectation(
        description="still on search",
        check=UrlContainsCheck(kind="url_contains", fragment="/search"),
    )
    specific = Expectation(
        description="the results arrived",
        check=TextVisibleCheck(kind="text_visible", text="1 record(s)"),
    )
    return capability(
        steps=[click(0, loose), click(1, loose), click(2, specific)],
        inputs=[],
        outputs=[],
    )


def test_a_run_that_stops_moving_is_reported_as_no_progress():
    # Every click resolves, every action succeeds, the first two expectations
    # hold -- and the page never changes. The third failure is where it surfaces,
    # and the diagnosis is the pattern rather than the local symptom.
    dead = page(SEARCH_URL, [element("e1", "button", "Find Member")])
    surface = FakeSurface([dead])

    result = run(surface, cap=stalled_capability(), params={})

    assert isinstance(result, HardFailure)
    assert result.kind is FailureKind.EXPECTATION_FAILED
    assert result.stuck_hint == StuckCondition.NO_PROGRESS_N_STEPS.value


def test_one_unchanged_step_is_not_yet_no_progress():
    # A single click that re-renders an identical page happens; the condition is
    # about a pattern, so one is not enough to claim it.
    dead = page(SEARCH_URL, [element("e1", "button", "Find Member")])
    surface = FakeSurface([dead])
    specific = Expectation(
        description="the results arrived",
        check=TextVisibleCheck(kind="text_visible", text="1 record(s)"),
    )
    only = [stalled_capability().steps[0].model_copy(update={"expectation": specific})]

    result = run(surface, cap=capability(steps=only, inputs=[], outputs=[]), params={})

    assert isinstance(result, HardFailure)
    assert result.stuck_hint is None


def test_a_page_that_keeps_changing_never_looks_stalled():
    moving = [
        page(SEARCH_URL, [element("e1", "button", "Find Member")], text_digest=f"page {i}")
        for i in range(8)
    ]
    surface = FakeSurface(moving)

    result = run(surface, cap=stalled_capability(), params={})

    assert isinstance(result, HardFailure)
    assert result.stuck_hint is None


def signed_out_capability() -> Capability:
    """Type a password, then go looking for something on the next page."""
    return capability(
        steps=[
            CapabilityStep(
                index=0,
                intent="type the passcode",
                action=ActionKind.TYPE,
                target=TargetSpec(role="textbox", name="Passcode", scope="main", match_index=0),
                value=CredentialRef(kind="credential", field="password"),
                risk=RiskTier.SAFE,
                expectation=Expectation(
                    description="signed in",
                    check=UrlContainsCheck(kind="url_contains", fragment="/search"),
                ),
            ),
            CapabilityStep(
                index=1,
                intent="read the balance",
                action=ActionKind.READ,
                target=TargetSpec(
                    role="StaticText", name="Balance", scope="main", match_index=0
                ),
                risk=RiskTier.SAFE,
                expectation=Expectation(
                    description="balance visible",
                    check=TextVisibleCheck(kind="text_visible", text="$918.40"),
                ),
            ),
        ],
        inputs=[],
        outputs=[],
    )


def test_a_password_field_after_signing_in_is_reported_as_auth_expired():
    # The session died mid-flow. At the step this looks like a control that is
    # not there, which would send an operator hunting for a renamed link when
    # what they actually need to do is sign back in.
    login = page("http://localhost:5000/login", [element("e1", "textbox", "Passcode")])
    signed_in = page(SEARCH_URL, [element("e9", "StaticText", "Balance")])
    signed_out = page("http://localhost:5000/login", [element("e2", "textbox", "Passcode")])
    surface = FakeSurface([login, signed_in, signed_out])

    result = run(surface, cap=signed_out_capability(), params={})

    assert isinstance(result, HardFailure)
    assert result.stuck_hint == StuckCondition.AUTH_EXPIRED.value


def test_a_password_field_before_signing_in_is_not_auth_expired():
    # The rule is narrow on purpose. A password box on the login page is the
    # login page, not a dead session -- otherwise every run would start expired.
    login = page("http://localhost:5000/login", [element("e1", "textbox", "Passcode")])
    surface = FakeSurface([login])
    only = [
        signed_out_capability().steps[0].model_copy(
            update={
                "value": None,
                "expectation": Expectation(
                    description="signed in",
                    check=TextVisibleCheck(kind="text_visible", text="Member Search"),
                ),
            }
        )
    ]

    result = run(surface, cap=capability(steps=only, inputs=[], outputs=[]), params={})

    assert isinstance(result, HardFailure)
    assert result.stuck_hint is None


def test_escalation_sends_the_operator_to_the_condition_the_hint_names():
    # The hint has to survive into the intervention, or the diagnosis was for
    # nobody. `kind` here is TARGET_NOT_FOUND, which maps to LOCATOR_UNRESOLVED.
    failure = HardFailure(
        kind=FailureKind.TARGET_NOT_FOUND,
        step_index=1,
        expected="a control",
        observed="a login page",
        stuck_hint=StuckCondition.AUTH_EXPIRED.value,
    )

    assert EscalationPolicy.condition_for(failure) is StuckCondition.AUTH_EXPIRED


def test_a_failure_with_no_hint_falls_back_to_its_kind():
    failure = HardFailure(
        kind=FailureKind.TARGET_NOT_FOUND, step_index=1, expected="a", observed="b"
    )

    assert EscalationPolicy.condition_for(failure) is StuckCondition.LOCATOR_UNRESOLVED


def test_both_engine_emitted_conditions_resolve_to_real_members():
    # A hint that did not name a member would silently fall back, which is the
    # quiet-rot failure this whole exercise is about.
    for hint in (StuckCondition.NO_PROGRESS_N_STEPS, StuckCondition.AUTH_EXPIRED):
        failure = HardFailure(
            kind=FailureKind.EXPECTATION_FAILED,
            step_index=0,
            expected="a",
            observed="b",
            stuck_hint=hint.value,
        )
        assert EscalationPolicy.condition_for(failure) is hint


def test_credentials_typed_but_not_yet_submitted_is_not_auth_expired():
    """The login page still showing its own password box is not an expiry.

    Found by escalating a real ParaBank replay. The capability types a
    username, types a password, then clicks "Log In" -- and the drifted twin
    renames that button, so the click cannot resolve and the run escalates
    from the login page. It escalated as `auth_expired`, and told the operator:

        "The session appears to have been signed out: a password field is on
         screen again partway through the flow. Sign back in..."

    Nothing had signed in. The run had not got past the login form, and the
    password box the detector saw was the one the flow had just typed into.

    `note_credential_used` set `signed_in` as soon as a step carrying a
    `CredentialRef` was *reached* -- but typing a password is not signing in,
    clicking submit is. So for the whole login sequence the detector was armed
    and looking at the very field it had just filled.

    That matters more than a mislabelled enum. `conditions.py` argues the enum
    earns its size because each member sends the operator somewhere different,
    and this sent them to re-authenticate a session that had never
    authenticated, while the actual fault -- a renamed button, named correctly
    in the `why` line right underneath -- went unmentioned in the headline.

    Being signed in is now observed rather than assumed: the password field has
    to have gone away at least once. That is the event "signed in" actually
    consists of, and it needs nothing from the capability to detect.
    """
    login = page("http://localhost:5000/login", [element("e1", "textbox", "Passcode")])
    # The submit control is not on the page -- the vendor renamed it -- so the
    # step after the credential step cannot resolve, exactly as on ParaBank.
    surface = FakeSurface([login, login, login])

    result = run(surface, cap=signed_out_capability(), params={})

    assert isinstance(result, HardFailure)
    assert result.stuck_hint != StuckCondition.AUTH_EXPIRED.value, (
        "a password box on the page the flow is still trying to log in from was read "
        "as a dead session, which sends the operator to sign in when what broke is the "
        "control they should have been pointed at"
    )
