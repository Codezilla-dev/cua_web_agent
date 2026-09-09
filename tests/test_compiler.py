"""Tests for the rules that decide what a capability *is*.

Two properties here are load-bearing, and both were bugs first.

An artifact's identity must not contain its own argument. `look_up_member_12345`
reads like a capability for one record, so a caller looking for the one that
handles member 22841 does not believe this is it, and recompiling the same goal
with a different example forks a second artifact for the same flow.

A parameterised capability's checkpoint must be able to tell one record from
another. The compiler's older output asserted that the text "Savings" was
visible -- true on every member's page. Ask for member 22841, land on member
12345, read the wrong balance, and that checkpoint reports success. A check that
cannot distinguish those two runs is not checking the thing the caller cares
about.
"""

from datetime import UTC, datetime

import pytest

from src.artifact.compiler import CompilerError, compile_capability, recorded_urls
from src.types import (
    ActionKind,
    ActResult,
    Decision,
    ElementVisibleCheck,
    Expectation,
    Intent,
    PerceptionCoverage,
    PolicyVerdict,
    RiskTier,
    RunOutcome,
    StepOutcome,
    StepRecord,
    TextVisibleCheck,
    Trace,
    UIElement,
    UIState,
    UrlContainsCheck,
)

GOAL = "look up member 12345 and read their savings balance"


def state(url: str, elements: list[UIElement] | None = None) -> UIState:
    elements = elements or []
    return UIState(
        url=url,
        title="page",
        elements=elements,
        text_digest="digest",
        coverage=PerceptionCoverage(
            ok=True, reason=None, interactive_count=len(elements), unnamed_ratio=0.0
        ),
        captured_at=datetime.now(UTC),
    )


def step(
    index: int,
    action: ActionKind,
    url: str,
    *,
    value: str | None = None,
    target_ref: str | None = None,
    target_description: str = "",
    elements: list[UIElement] | None = None,
    expectation_text: str = "something",
    read_value: str | None = None,
    expectation: Expectation | None = None,
) -> StepRecord:
    """One executed step. Everything the compiler does not read is stubbed."""
    return StepRecord(
        index=index,
        started_at=datetime.now(UTC),
        duration_ms=1,
        state_before=state(url, elements),
        decision=Decision(
            intent=Intent(
                reasoning="because",
                intent=f"step {index}",
                action=action,
                target_ref=target_ref,
                target_description=target_description,
                value=value,
                risk=RiskTier.SAFE,
                expectation=expectation
                or Expectation(
                    description=f"{expectation_text} is visible",
                    check=TextVisibleCheck(kind="text_visible", text=expectation_text),
                ),
            ),
            model="test",
            latency_ms=1,
        ),
        policy=PolicyVerdict(
            allowed=True, tier=RiskTier.SAFE, rule="test", reason="ok", disposition="allow"
        ),
        act=ActResult(ok=True, action=action, duration_ms=1, read_value=read_value),
        outcome=StepOutcome.OK,
    )


def trace_of(steps: list[StepRecord], goal: str = GOAL) -> Trace:
    return Trace(
        run_id="20260909T000000Z-test",
        goal=goal,
        target_url="http://localhost:5000",
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        outcome=RunOutcome.COMPLETED,
        steps=steps,
    )


def lookup_trace(final_url: str = "http://localhost:5000/member/12345") -> Trace:
    """The shape of the real recording: type the id, click through, read, done."""
    search_box = UIElement(ref="e1", role="textbox", name="Member ID or Name")
    return trace_of(
        [
            step(
                0,
                ActionKind.TYPE,
                "http://localhost:5000/search",
                value="12345",
                target_ref="e1",
                target_description="Member ID or Name textbox",
                elements=[search_box],
            ),
            step(1, ActionKind.CLICK, "http://localhost:5000/search", target_description="Find"),
            step(
                2,
                ActionKind.READ,
                final_url,
                target_description="Savings account balance value",
                read_value="$4,812.55",
            ),
            step(3, ActionKind.DONE, final_url, expectation_text="$4,812.55"),
        ]
    )


# --- identity ---------------------------------------------------------------
def test_no_recorded_argument_value_appears_in_the_id_or_the_title():
    # The invariant, stated once. A capability keeps the shape of the goal and
    # gives back the values that vary.
    capability = compile_capability(lookup_trace())

    assert capability.inputs, "this trace is supposed to produce a parameter"
    for spec in capability.inputs:
        assert spec.example
        assert spec.example not in capability.id
        assert spec.example not in capability.title


def test_the_id_is_derived_from_the_goal_without_its_arguments():
    capability = compile_capability(lookup_trace())

    assert capability.id == "look_up_member_read"


def test_the_title_templates_the_argument_rather_than_dropping_it():
    # The title is what a human and an agent read to decide whether this is the
    # right capability, so it has to say what it needs as well as what it does.
    capability = compile_capability(lookup_trace())

    assert capability.title == (
        "look up member {{member_id_or_name}} and read their savings balance"
    )


def test_two_recordings_of_the_same_goal_share_an_id():
    # The consequence of deriving identity from the flow rather than the
    # argument: recompiling overwrites rather than forking, which is what makes
    # `version` mean anything.
    first = compile_capability(lookup_trace())
    second = compile_capability(lookup_trace())

    assert first.id == second.id


def test_an_explicit_id_still_wins():
    capability = compile_capability(lookup_trace(), capability_id="chosen_by_hand")

    assert capability.id == "chosen_by_hand"


# --- checkpoint -------------------------------------------------------------
def test_a_parameterised_capability_gets_a_checkpoint_that_names_its_parameter():
    capability = compile_capability(lookup_trace())

    assert capability.checkpoint.check.kind == "url_contains"
    assert capability.checkpoint.check.fragment == "{{member_id_or_name}}"


def test_the_checkpoint_refuses_to_compile_when_no_parameter_identifies_the_record():
    # The falsification this rule exists for. If the run finished somewhere the
    # requested value never appears, there is no assertion available that can
    # tell the right record from the wrong one -- and emitting a checkpoint that
    # cannot discriminate is worse than refusing, because it reports success.
    with pytest.raises(CompilerError) as error:
        compile_capability(lookup_trace(final_url="http://localhost:5000/summary"))

    assert "distinguish one record from another" in str(error.value)


def test_a_capability_with_no_parameters_keeps_the_recorded_checkpoint():
    # The rule is about parameterised capabilities. A flow that takes no input
    # has nothing to bind, and its recorded checkpoint is not overfitted to
    # anything the caller varies.
    capability = compile_capability(
        trace_of(
            [
                step(0, ActionKind.CLICK, "http://localhost:5000/", target_description="Reports"),
                step(1, ActionKind.DONE, "http://localhost:5000/reports"),
            ],
            goal="open the reports page",
        )
    )

    assert not capability.inputs
    assert capability.checkpoint.check.kind == "text_visible"


# --- what a later pass is allowed to assert ---------------------------------
def test_recorded_urls_are_keyed_by_the_capabilitys_own_step_numbering():
    # The compiler renumbers after dropping failed attempts, so the trace's
    # indices do not line up with the artifact's. A later pass checking a URL
    # claim against the wrong step would validate it against the wrong page.
    trace = lookup_trace()
    capability = compile_capability(trace)
    per_step, final = recorded_urls(trace)

    assert set(per_step) <= {s.index for s in capability.steps}
    assert final == "http://localhost:5000/member/12345"
    # A URL is recorded against the step it *followed*, not the one it preceded.
    # Typing the id leaves the session on /search, and the click after it is what
    # reaches the record -- so nothing downstream may template a member id into
    # an assertion about step 0, and it may about step 1.
    assert per_step[0] == "http://localhost:5000/search"
    assert "12345" not in per_step[0]
    assert per_step[1] == "http://localhost:5000/member/12345"


def test_a_checkpoint_that_quotes_the_answer_as_an_element_name_is_still_rewritten():
    """`element_visible` carries a literal too, and it was not being looked at.

    Found on ParaBank. The goal was "Find the total money in all accounts";
    the planner ended the run with

        {"kind": "element_visible", "role": "StaticText",
         "name_contains": "$515.50"}

    which quotes the answer exactly the way `_resolve_checkpoint` exists to
    catch -- except `_quoted_text` only read `TextVisibleCheck.text` and
    `FieldHasValueCheck.expected`, so it returned None and the checkpoint stood.
    The compiled capability asserted a specific balance on a public demo site,
    and would report a hard failure the first time that figure moved.

    Ten lines away, `_binds_a_parameter` already listed `name_contains` among
    the attributes that hold a literal. The two functions disagreed about what
    counts as quoting, and the checkpoint path was the one that was wrong.

    `url_contains.fragment` is deliberately still not treated as quoting: a URL
    carrying a value the run read is how a lookup identifies its record, which
    is a checkpoint worth binding to a parameter rather than one to discard.
    """
    capability = compile_capability(
        trace_of(
            [
                step(0, ActionKind.CLICK, "https://bank.test/", target_description="Log In"),
                step(
                    1,
                    ActionKind.READ,
                    "https://bank.test/overview",
                    target_description="total account balance",
                    read_value="$515.50",
                    expectation_text="$515.50",
                ),
                step(
                    2,
                    ActionKind.DONE,
                    "https://bank.test/overview",
                    expectation=Expectation(
                        description="the total balance is shown",
                        check=ElementVisibleCheck(
                            kind="element_visible", role="StaticText", name_contains="$515.50"
                        ),
                    ),
                ),
            ],
            goal="find the total money in all accounts",
        )
    )

    assert "$515.50" not in capability.checkpoint.model_dump_json(), (
        "the checkpoint still pins the recorded balance, so replay asserts a figure "
        f"that changes: {capability.checkpoint.model_dump_json()}"
    )


def test_a_read_step_does_not_assert_the_value_it_is_about_to_return():
    """A `read` expectation quoting what it read is tautological and brittle.

    Found on the compiled ParaBank capability, after the checkpoint bug above
    was fixed. The checkpoint came out structural -- "the Accounts Overview
    heading is present" -- and step 3 still said:

        {"kind": "element_visible", "role": "StaticText",
         "name_contains": "$515.50"}

    Replay verifies every step, not only the checkpoint, so that assertion
    fails the moment a public demo bank's balance moves. It also asserts
    nothing worth asserting: the step's whole job is to return that value, so
    checking it first only says "the number I am about to read is the number I
    read last time".

    `generalize.apply` skipped `READ` steps outright, so the demotion ladder
    never looked at them. It did not need a model to: "the expectation quotes
    what this step's own `read` returned" is decidable by comparing two
    recorded strings, which is why the rule belongs in the deterministic
    compiler and not in the semantic pass.

    What it demotes to is the anchor's own claim -- the target is already
    located as "one past the element named Total", so asserting that `Total` is
    on the page is the structural fact the step depends on. That survives the
    balance changing, which is the entire point.
    """
    total_label = UIElement(ref="e1", role="StaticText", name="Total")
    total_value = UIElement(ref="e2", role="StaticText", name="$515.50")
    overview = [total_label, total_value]

    capability = compile_capability(
        trace_of(
            [
                step(0, ActionKind.CLICK, "https://bank.test/", target_description="Log In"),
                step(
                    1,
                    ActionKind.READ,
                    "https://bank.test/overview",
                    target_ref="e2",
                    target_description="total account balance",
                    elements=overview,
                    read_value="$515.50",
                    expectation=Expectation(
                        description="the total balance is visible",
                        check=ElementVisibleCheck(
                            kind="element_visible", role="StaticText", name_contains="$515.50"
                        ),
                    ),
                ),
                step(2, ActionKind.DONE, "https://bank.test/overview"),
            ],
            goal="find the total money in all accounts",
        )
    )

    read_step = next(s for s in capability.steps if s.action is ActionKind.READ)

    assert "$515.50" not in read_step.expectation.model_dump_json(), (
        "the read step still asserts the balance it was recorded with, so replay breaks "
        f"when that figure moves: {read_step.expectation.model_dump_json()}"
    )
    # Demoted, not deleted: a step with no expectation is one replay takes on
    # faith, which is what declaring before acting exists to prevent.
    assert read_step.expectation.check.kind == "element_visible"
    assert read_step.expectation.check.name_contains == "Total"


def test_a_read_step_whose_expectation_says_something_else_is_left_alone():
    # The rule is narrow: it fires only when the expectation quotes the value
    # this step's own read returned. An expectation asserting the page it
    # landed on is already structural and must survive untouched.
    total_label = UIElement(ref="e1", role="StaticText", name="Total")
    total_value = UIElement(ref="e2", role="StaticText", name="$515.50")

    capability = compile_capability(
        trace_of(
            [
                step(0, ActionKind.CLICK, "https://bank.test/", target_description="Log In"),
                step(
                    1,
                    ActionKind.READ,
                    "https://bank.test/overview",
                    target_ref="e2",
                    target_description="total account balance",
                    elements=[total_label, total_value],
                    read_value="$515.50",
                    expectation=Expectation(
                        description="the overview is open",
                        check=UrlContainsCheck(kind="url_contains", fragment="overview"),
                    ),
                ),
                step(2, ActionKind.DONE, "https://bank.test/overview"),
            ],
            goal="find the total money in all accounts",
        )
    )

    read_step = next(s for s in capability.steps if s.action is ActionKind.READ)

    assert read_step.expectation.check.kind == "url_contains"
    assert read_step.expectation.check.fragment == "overview"
