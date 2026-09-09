"""The one judgement in compilation that rules cannot make.

The compiler is deterministic and stays that way. It owns everything
mechanically derivable from a trace: which steps ran, what they did, which
control they touched, in what order. Those are facts, and rules read facts
better than a model does -- every structural bug found while building replay
(steps selected by the wrong signal, credentials bound inconsistently, a
checkpoint falling through to success) is a mistake a model would have made
silently.

What rules read badly is meaning. A repaired expectation from a discovery run
may assert `text_visible: "Dolores Haze"` -- the name of the member that
happened to be recorded. That capability replays for member 12345 and fails for
22841, not because the flow is wrong but because the assertion was fitted to one
record. Catching that means answering "is this string page furniture or is it a
datum", and that question has no syntactic signature: a currency pattern catches
`$4,812.55`, but nothing distinguishes `Dolores Haze` from a column heading
except knowing what a person's name looks like.

So the split follows the evidence rather than a preference. Rules build the
skeleton; one narrow model call labels the expectations; and a validator refuses
anything the trace does not support. Three properties fall out of that shape:

**Replay still never sees a model.** This runs once, at compile time, offline.
`src/replay/` cannot import an LLM client and a test enforces it. Nothing here
changes that.

**The artifact cannot get worse.** `distil()` is optional and every failure --
no client, a bad response, an ungrounded label -- returns `None`, which leaves
the compiler's own output exactly as it was.

**A label is a claim about the trace, not a free-form opinion.** The validator
checks every step index against the compiled capability and drops the rest.
"""

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from src.artifact.models import Capability
from src.llm import apply_sampling

TOOL_NAME = "label_expectations"

# Kept small on purpose. A large schema is where structured output starts
# failing, and this project has already measured that: the planner needed a
# corrective retry for schema errors, and a prompt rule was ignored outright
# until it was enforced in code. Ask for one judgement, not a whole artifact.
SYSTEM_PROMPT = """\
You are reviewing a UI automation capability that was recorded once and will be \
replayed many times with different inputs.

Each step carries an expectation that replay asserts after the action. Some of \
those expectations describe the *structure* of the page -- headings, labels, \
button text, URL shape -- and hold for any input. Others accidentally quote the \
*data of the specific record* that happened to be used during recording -- a \
person's name, an account balance, an ID -- and can only ever pass for that one \
record.

Label each expectation:
  "structural"  - holds for any valid input
  "record_data" - quotes data belonging to the recorded record

Judge the text of the expectation, not whether the step seems important. A \
label of "record_data" means replay will stop asserting that text and check the \
step's control instead. When you are unsure, answer "structural": weakening a \
correct assertion costs more than leaving one narrow one in place.
"""


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExpectationLabel(Base):
    """One judgement about one step's expectation."""

    step_index: int
    classification: Literal["structural", "record_data"]
    # Not decoration: this is what a reviewer reads when deciding whether the
    # model was right, and it is written into the compile log.
    reason: str = ""


class Distillation(Base):
    """Everything the model is asked to decide. Deliberately one thing."""

    labels: list[ExpectationLabel]
    checkpoint_is_record_specific: bool = False
    checkpoint_reason: str = ""

    def record_data_steps(self) -> set[int]:
        return {
            label.step_index
            for label in self.labels
            if label.classification == "record_data"
        }


def build_tool_schema() -> dict[str, Any]:
    """Derived from `Distillation`, so the schema cannot drift from the type."""
    return {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": (
                "Label each step's expectation as structural or record_data, and say "
                "whether the checkpoint is specific to the recorded record."
            ),
            "parameters": Distillation.model_json_schema(),
        },
    }


def describe_for_review(capability: Capability) -> str:
    """The compiled skeleton, as the model sees it.

    Only what is needed to judge the expectations: the goal, and per step its
    index, action, target name, and the expectation's own text. No credentials,
    no page dumps, no transcript -- a smaller prompt is both cheaper and easier
    for the model to answer correctly.
    """
    lines = [f"Goal: {capability.provenance.goal}", "", "Steps:"]
    for step in capability.steps:
        target = step.target.name if step.target else "-"
        check = step.expectation.check.model_dump()
        check.pop("case_sensitive", None)
        lines.append(
            f"  [{step.index}] {step.action.value} on {target!r}\n"
            f"        expectation: {step.expectation.description}\n"
            f"        check: {json.dumps(check)}"
        )
    checkpoint = capability.checkpoint.check.model_dump()
    checkpoint.pop("case_sensitive", None)
    lines += [
        "",
        f"Checkpoint: {capability.checkpoint.description}",
        f"  check: {json.dumps(checkpoint)}",
        "",
        f"Inputs: {[spec.name for spec in capability.inputs]}",
    ]
    return "\n".join(lines)


def validate_against(distillation: Distillation, capability: Capability) -> Distillation:
    """Drop any label that does not correspond to a step in this capability.

    The model is answering about a document it was shown, so a label for a step
    that does not exist means it invented one -- and a label that is silently
    kept would be applied to nothing or, worse, to the wrong step. Dropping is
    right rather than raising: the labels that *are* grounded remain useful.
    """
    known = {step.index for step in capability.steps}
    grounded = [label for label in distillation.labels if label.step_index in known]
    return distillation.model_copy(update={"labels": grounded})


def distil(
    capability: Capability,
    client: Any,
    model: str,
    url: str,
    timeout_s: float = 60.0,
) -> Distillation | None:
    """Ask the model to label the expectations. `None` on any failure.

    Returning `None` rather than raising is the whole safety argument for this
    pass: the caller keeps the compiler's deterministic output, so the artifact
    is never worse for having tried.
    """
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": describe_for_review(capability)},
        ],
        "tools": [build_tool_schema()],
        "tool_choice": {"type": "function", "function": {"name": TOOL_NAME}},
    }
    # The reason this pass was dead: it sent `temperature: 0` and no
    # `reasoning_effort`, which the gpt-5 family rejects with a 400 when a
    # function tool is forced. The failure was swallowed by the no-op handling
    # below, so every run printed "semantic pass unavailable" and carried on.
    apply_sampling(payload, model)
    try:
        response = client.post(url, json=payload, timeout=timeout_s)
        response.raise_for_status()
        message = response.json()["choices"][0]["message"]
        arguments = message["tool_calls"][0]["function"]["arguments"]
        distillation = Distillation.model_validate_json(arguments)
    except (KeyError, IndexError, TypeError, ValidationError, json.JSONDecodeError):
        return None
    except Exception:
        # Network, timeout, auth, rate limit. A capability that compiles without
        # the semantic pass is the documented fallback, not an outage.
        return None
    return validate_against(distillation, capability)
