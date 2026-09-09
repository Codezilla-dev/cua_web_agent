"""What a given model needs a chat-completions request to look like.

Two modules call an LLM: `agent/planner.py`, which chooses the next step, and
`artifact/semantics.py`, which labels a compiled capability's expectations.
They ask different questions and share nothing else -- but the request *shape*
a model demands is a property of the model, not of the question, so both need
the same answer to it.

They did not share it, and the cost was a silently dead code path. The planner
knew that the reasoning families reject `temperature` and refuse a forced
function tool unless reasoning is switched off; the semantic pass did not, sent
`temperature: 0` with no `reasoning_effort`, and got a 400 on every call. Its
own error handling is "any failure is a no-op, keep the compiler's
expectations", which is the right design and which turned a configuration bug
into silence -- every run printed "semantic pass unavailable" and nobody read it
as a defect.

So the rule lives here once. A module that adds a third caller gets it for free
rather than rediscovering it in production.
"""

from typing import Any

# The o-series and the gpt-5 family differ from gpt-4.1 in two ways that matter
# on /chat/completions: they reject `temperature` other than 1, and they refuse
# a forced function tool unless reasoning is explicitly switched off. Detected
# from the id so no extra configuration is needed. The match is deliberately
# loose -- a leading "provider/" segment (OpenRouter) is stripped first, then a
# prefix check, so `gpt-5.6-luna` and `openai/o3-mini` are both recognised.
REASONING_MODEL_PREFIXES = ("o1", "o3", "o4", "gpt-5")


def is_reasoning_model(model_id: str) -> bool:
    """True for the families that need the reasoning request shape."""
    base = model_id.split("/", 1)[-1].strip().lower()
    return base.startswith(REASONING_MODEL_PREFIXES)


def apply_sampling(payload: dict[str, Any], model_id: str) -> dict[str, Any]:
    """Set whichever of `reasoning_effort` / `temperature` this model accepts.

    Mutates and returns `payload` so it can be used inline at a call site that
    is already building one.

    `temperature: 0` is what makes the discovery loop reproducible, so it is the
    default and is only dropped where the model forbids it. `reasoning_effort:
    "none"` is not a preference either -- on this endpoint it is what allows a
    forced function tool at all.
    """
    if is_reasoning_model(model_id):
        payload["reasoning_effort"] = "none"
    else:
        payload["temperature"] = 0
    return payload
