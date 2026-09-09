"""Tests for the semantic pass and the demotion it drives.

The load-bearing property is not "the model is right". It is that the pipeline
is *safe when the model is wrong or absent*: every failure path leaves the
deterministic compiler's output untouched, and no label is applied that the
compiled capability does not support.
"""

from datetime import UTC, datetime

from src.artifact import generalize
from src.artifact.compiler import CREDENTIAL_PLACEHOLDER
from src.artifact.models import (
    Capability,
    CapabilityStep,
    ParamSpec,
    Provenance,
    SurfaceRef,
    TargetSpec,
)
from src.artifact.semantics import (
    Distillation,
    ExpectationLabel,
    describe_for_review,
    distil,
    validate_against,
)
from src.types import (
    ActionKind,
    Expectation,
    FieldHasValueCheck,
    RiskTier,
    TextVisibleCheck,
    UrlContainsCheck,
)

RECORD_PAGE = "http://localhost:5000/member/12345"
SEARCH_PAGE = "http://localhost:5000/search"


def make_step(
    index,
    action=ActionKind.CLICK,
    target_name: str | None = "Open Record",
    text="Dolores Haze",
):
    return CapabilityStep(
        index=index,
        intent=f"step {index}",
        action=action,
        target=(
            TargetSpec(role="link", name=target_name, scope="main", match_index=0)
            if target_name
            else None
        ),
        risk=RiskTier.SAFE,
        expectation=Expectation(
            description=f"{text} is visible",
            check=TextVisibleCheck(kind="text_visible", text=text),
        ),
    )


def make_capability(steps=None, checkpoint_text="Dolores Haze"):
    return Capability(
        id="look_up_member",
        title="Look up member",
        surface=SurfaceRef(kind="web", entry_path="/login", recorded_origin="http://localhost:5000"),
        inputs=[ParamSpec(name="member_id_or_name", example="12345")],
        outputs=[],
        steps=steps if steps is not None else [make_step(0)],
        checkpoint=Expectation(
            description="record page reached",
            check=TextVisibleCheck(kind="text_visible", text=checkpoint_text),
        ),
        provenance=Provenance(
            discovery_run_id="run-1",
            goal="look up member 12345 and read their savings balance",
            model="gpt-4.1",
            recorded_at=datetime.now(UTC),
            source_step_count=9,
            retried_step_count=1,
        ),
    )


def labels(*pairs, checkpoint=False):
    return Distillation(
        labels=[
            ExpectationLabel(step_index=i, classification=c, reason="test")
            for i, c in pairs
        ],
        checkpoint_is_record_specific=checkpoint,
    )


# --------------------------------------------------------------------------- #
# Grounding: a label is a claim about the trace
# --------------------------------------------------------------------------- #
def test_a_label_for_a_step_that_does_not_exist_is_dropped():
    capability = make_capability(steps=[make_step(0), make_step(1)])
    invented = labels((0, "record_data"), (7, "record_data"))

    grounded = validate_against(invented, capability)

    assert [label.step_index for label in grounded.labels] == [0]


def test_the_review_document_never_contains_a_transcript_or_a_secret():
    capability = make_capability()

    described = describe_for_review(capability)

    assert "Dolores Haze" in described  # the thing being judged
    assert "reasoning" not in described
    assert "password" not in described.lower()


# --------------------------------------------------------------------------- #
# Demotion
# --------------------------------------------------------------------------- #
def test_a_non_navigating_step_is_demoted_to_its_own_control():
    # Typing does not move the page, so the field is still there afterwards.
    step = make_step(0, action=ActionKind.TYPE, target_name="Member ID or Name")
    capability = make_capability(steps=[step])

    result, notes = generalize.apply(capability, labels((0, "record_data")))

    check = result.steps[0].expectation.check
    assert check.kind == "element_visible"
    assert check.name_contains == "Member ID or Name"
    assert "step 0" in notes[0]


def test_a_click_is_never_demoted_to_the_control_it_just_clicked():
    # Regression, and the reason the ladder is action-dependent at all. An
    # expectation is asserted *after* the action: step 5 of the real capability
    # clicks "Open Record", and afterwards the session is on the member page
    # where that link does not exist. Demoting to element_visible there produced
    # an expectation that failed for *every* input, including the recorded one --
    # a generalisation strictly worse than the overfitting it replaced.
    capability = make_capability(steps=[make_step(0, action=ActionKind.CLICK)])

    result, _ = generalize.apply(
        capability, labels((0, "record_data")), {0: RECORD_PAGE}, RECORD_PAGE
    )

    check = result.steps[0].expectation.check
    assert check.kind != "element_visible"
    assert check.kind == "url_contains"


def test_a_navigating_step_asserts_the_requested_record_not_the_recorded_one():
    capability = make_capability(steps=[make_step(0, action=ActionKind.CLICK)])

    # The recording says this step landed on the member page, so the member id
    # is a fact about the URL and templating it is a claim the evidence backs.
    result, _ = generalize.apply(
        capability, labels((0, "record_data")), {0: RECORD_PAGE}, RECORD_PAGE
    )

    # Templated, so it fails if the flow lands on the wrong record -- which is a
    # stronger claim than "we are on some page".
    check = result.steps[0].expectation.check
    assert isinstance(check, UrlContainsCheck)
    assert check.fragment == "{{member_id_or_name}}"


def test_a_step_that_did_not_land_on_the_record_is_never_templated():
    # Regression. The demoter used to template the first input that had an
    # example, whatever page the step reached. Step 2 of the real capability
    # submits the login form and lands on "/search", which contains no member
    # id -- so that assertion would have failed on every invocation, including
    # the recorded one. A URL claim is only honest about a value the URL had.
    capability = make_capability(steps=[make_step(0, action=ActionKind.CLICK)])

    result, _ = generalize.apply(
        capability, labels((0, "record_data")), {0: SEARCH_PAGE}, SEARCH_PAGE
    )

    check = result.steps[0].expectation.check
    assert check.kind == "url_contains"
    assert "{{" not in check.fragment
    assert check.fragment == "/login"  # the entry path: weaker, but true


def test_no_recorded_url_means_no_url_claim_is_invented():
    # Every failure in this module has to be a no-op. With nothing to check a
    # claim against, the ladder skips the templating rung rather than guessing.
    capability = make_capability(steps=[make_step(0, action=ActionKind.CLICK)])

    result, _ = generalize.apply(capability, labels((0, "record_data")))

    check = result.steps[0].expectation.check
    assert "{{" not in getattr(check, "fragment", "")


def test_a_structural_expectation_is_left_exactly_as_it_was():
    capability = make_capability(steps=[make_step(0, text="Account Summary")])

    result, notes = generalize.apply(capability, labels((0, "structural")))

    assert result.steps[0].expectation == capability.steps[0].expectation
    assert notes == []


def test_demotion_never_deletes_an_expectation():
    # A step with no expectation is a step replay takes on faith, which is the
    # thing the whole declare-before-acting design exists to prevent.
    capability = make_capability(steps=[make_step(0), make_step(1)])

    result, _ = generalize.apply(capability, labels((0, "record_data"), (1, "record_data")))

    assert all(step.expectation is not None for step in result.steps)


def test_a_read_step_is_left_alone_because_replay_already_handles_it():
    # Replay asserts that a read returned something, rather than the recorded
    # value. Rewriting it here would be a second mechanism for a solved problem.
    step = make_step(0, action=ActionKind.READ, target_name="$4,812.55", text="$4,812.55")
    capability = make_capability(steps=[step])

    result, notes = generalize.apply(capability, labels((0, "record_data")))

    assert result.steps[0].expectation == step.expectation
    assert notes == []


def test_a_step_with_no_control_falls_back_to_the_url_shape():
    capability = make_capability(steps=[make_step(0, action=ActionKind.NAVIGATE, target_name=None)])
    capability = capability.model_copy(update={"inputs": []})

    result, _ = generalize.apply(capability, labels((0, "record_data")))

    check = result.steps[0].expectation.check
    assert check.kind == "url_contains"
    assert check.fragment == "/login"


def test_the_checkpoint_is_retargeted_at_the_requested_record_not_the_recorded_one():
    capability = make_capability(checkpoint_text="Dolores Haze")

    result, notes = generalize.apply(
        capability, labels((0, "structural"), checkpoint=True), {}, RECORD_PAGE
    )

    check = result.checkpoint.check
    assert isinstance(check, UrlContainsCheck)
    # Templated, so replay binds the caller's input rather than the recorded one.
    assert check.fragment == "{{member_id_or_name}}"
    assert any("checkpoint" in note for note in notes)


def test_an_unimprovable_expectation_is_reported_rather_than_silently_weakened():
    # No parameter to template and no path worth asserting: there is nothing
    # honest to say, so the original stands and the caller is told.
    capability = make_capability(
        steps=[make_step(0, action=ActionKind.NAVIGATE, target_name=None)],
    )
    capability = capability.model_copy(
        update={
            "surface": SurfaceRef(kind="web", entry_path="/", recorded_origin="http://x"),
            "inputs": [],
        }
    )

    result, notes = generalize.apply(capability, labels((0, "record_data")))

    assert result.steps[0].expectation == capability.steps[0].expectation
    assert "left unchanged" in notes[0]


# --------------------------------------------------------------------------- #
# Failure is always a no-op
# --------------------------------------------------------------------------- #
class FailingClient:
    def __init__(self, error):
        self._error = error

    def post(self, *_args, **_kwargs):
        raise self._error


class BadResponseClient:
    def __init__(self, payload):
        self._payload = payload

    def post(self, *_args, **_kwargs):
        return self

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_a_network_failure_leaves_the_compiler_output_alone():
    assert distil(make_capability(), FailingClient(TimeoutError("no route")), "m", "u") is None


def test_a_response_of_the_wrong_shape_is_ignored():
    assert distil(make_capability(), BadResponseClient({"nope": True}), "m", "u") is None


def test_a_response_that_fails_schema_validation_is_ignored():
    payload = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {"function": {"arguments": '{"labels": [{"step_index": "x"}]}'}}
                    ]
                }
            }
        ]
    }
    assert distil(make_capability(), BadResponseClient(payload), "m", "u") is None


def test_a_valid_response_is_grounded_before_it_is_returned():
    payload = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "function": {
                                "arguments": (
                                    '{"labels": [{"step_index": 0, "classification": '
                                    '"record_data", "reason": "a person\'s name"}, '
                                    '{"step_index": 42, "classification": "record_data", '
                                    '"reason": "invented"}], '
                                    '"checkpoint_is_record_specific": true}'
                                )
                            }
                        }
                    ]
                }
            }
        ]
    }

    result = distil(make_capability(), BadResponseClient(payload), "m", "u")

    assert result is not None
    assert [label.step_index for label in result.labels] == [0]
    assert result.checkpoint_is_record_specific is True


def test_a_check_that_already_follows_the_input_is_never_demoted():
    # The semantic pass is not deterministic: across two runs on the same
    # artifact the same model called these steps "structural" once and
    # "record_data" once. A check whose expected value is a parameter template
    # cannot be fitted to the recorded record -- it contains no recorded value
    # at all -- so the rule overrules the label.
    step = CapabilityStep(
        index=0,
        intent="type the member id",
        action=ActionKind.TYPE,
        target=TargetSpec(role="textbox", name="Member ID or Name"),
        risk=RiskTier.SAFE,
        expectation=Expectation(
            description="the field holds the requested id",
            check=FieldHasValueCheck(
                kind="field_has_value",
                name_contains="Member ID or Name",
                expected="{{member_id_or_name}}",
            ),
        ),
    )
    capability = make_capability(steps=[step])

    result, notes = generalize.apply(capability, labels((0, "record_data")))

    assert result.steps[0].expectation == step.expectation
    assert "kept" in notes[0]


def test_a_credential_check_is_never_demoted_either():
    step = CapabilityStep(
        index=0,
        intent="type the password",
        action=ActionKind.TYPE,
        target=TargetSpec(role="textbox", name="Passcode"),
        risk=RiskTier.SAFE,
        expectation=Expectation(
            description="the passcode field is filled",
            check=FieldHasValueCheck(
                kind="field_has_value", name_contains="Passcode", expected=CREDENTIAL_PLACEHOLDER
            ),
        ),
    )
    capability = make_capability(steps=[step])

    result, _ = generalize.apply(capability, labels((0, "record_data")))

    assert result.steps[0].expectation == step.expectation
