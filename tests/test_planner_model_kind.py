"""Request shape depends on the model family.

`_call_model` sends `temperature=0` to gpt-4.1-class models and, to the
o-series / gpt-5 reasoning family, sends `reasoning_effort="none"` and no
temperature instead -- those models 400 on `temperature != 1` and refuse a
forced function tool while reasoning is on. `_is_reasoning_model` is the pure
id check that picks the branch, so it is tested the same way the other pure
helpers are.
"""

from __future__ import annotations

import pytest

from src.agent.planner import _is_reasoning_model

REASONING_IDS = [
    "gpt-5",
    "gpt-5.6-luna",
    "gpt-5-mini",
    "o1",
    "o3-mini",
    "o4-mini",
    "openai/gpt-5.6-luna",  # OpenRouter-style provider prefix
    "GPT-5.6-LUNA",  # case-insensitive
]

NON_REASONING_IDS = [
    "gpt-4.1",
    "gpt-4o",
    "gpt-4.1-mini",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "google/gemma-4-31b-it:free",
    "",
]


@pytest.mark.parametrize("model_id", REASONING_IDS)
def test_reasoning_family_is_detected(model_id: str) -> None:
    assert _is_reasoning_model(model_id) is True


@pytest.mark.parametrize("model_id", NON_REASONING_IDS)
def test_other_models_are_not_flagged(model_id: str) -> None:
    assert _is_reasoning_model(model_id) is False
