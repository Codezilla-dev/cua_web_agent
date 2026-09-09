"""Both LLM callers must shape the request the way the model demands.

`agent/planner.py` and `artifact/semantics.py` ask different questions and share
nothing else, but the request *shape* a model requires is a property of the
model rather than of the question. They each had their own copy of that
knowledge and only one copy was right: the semantic pass sent `temperature: 0`
with no `reasoning_effort`, which the gpt-5 family rejects with a 400 whenever a
function tool is forced.

The failure was invisible. `distil` is designed so that any failure is a no-op
-- the compiler's expectations stand -- which is correct, and which turned a
configuration bug into a line of output nobody read as a defect. The whole
generalisation pass was dead for every reasoning model.
"""

import ast
from pathlib import Path

import pytest

from src.llm import apply_sampling, is_reasoning_model

SRC = Path(__file__).resolve().parent.parent / "src"
CALLERS = [SRC / "agent" / "planner.py", SRC / "artifact" / "semantics.py"]


@pytest.mark.parametrize(
    "model_id",
    ["gpt-5.6-luna", "gpt-5", "o1-preview", "o3-mini", "openai/o4-mini", "OPENAI/GPT-5.6"],
)
def test_the_reasoning_families_are_recognised(model_id):
    assert is_reasoning_model(model_id)


@pytest.mark.parametrize("model_id", ["gpt-4.1", "gpt-4o", "vendor/llama-3.3-70b", "claude-opus-5"])
def test_ordinary_models_are_not(model_id):
    assert not is_reasoning_model(model_id)


def test_a_reasoning_model_gets_reasoning_effort_and_no_temperature():
    # `temperature` is a 400 for this family, and a forced function tool is
    # refused on /chat/completions unless reasoning is switched off.
    payload = apply_sampling({}, "gpt-5.6-luna")

    assert payload == {"reasoning_effort": "none"}


def test_an_ordinary_model_gets_temperature_zero_and_no_reasoning_effort():
    # Determinism for the discovery loop is the default; it is only given up
    # where the model forbids it.
    payload = apply_sampling({}, "gpt-4.1")

    assert payload == {"temperature": 0}


def test_apply_sampling_leaves_the_rest_of_the_payload_alone():
    payload = apply_sampling({"model": "gpt-4.1", "tools": ["x"]}, "gpt-4.1")

    assert payload["model"] == "gpt-4.1" and payload["tools"] == ["x"]


@pytest.mark.parametrize("path", CALLERS, ids=lambda p: p.name)
def test_every_llm_caller_goes_through_the_shared_rule(path):
    source = path.read_text(encoding="utf-8")

    assert "apply_sampling" in source, (
        f"{path.name} builds a chat-completions payload without the shared "
        "sampling rule, which is exactly how the semantic pass came to 400 in "
        "silence on every reasoning model"
    )


@pytest.mark.parametrize("path", CALLERS, ids=lambda p: p.name)
def test_no_caller_hardcodes_the_sampling_parameters_itself(path):
    # A literal `"temperature": 0` in a request payload means someone has
    # re-introduced the duplicate, and it will be wrong for the next family.
    tree = ast.parse(path.read_text(encoding="utf-8"))
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }

    assert "temperature" not in literals, f"{path.name} sets `temperature` directly"
    assert "reasoning_effort" not in literals, f"{path.name} sets `reasoning_effort` directly"
