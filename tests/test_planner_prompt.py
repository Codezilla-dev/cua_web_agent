"""The prompt has to ask for everything the schema requires.

Found by running against ParaBank: the model returned tool calls that were
complete except for `reasoning` and `intent`, twice in a row, and the run died
with `RunOutcome.ERROR` two steps in. Those are exactly the two required fields
the system prompt never mentioned -- it documented `target_ref`, `expectation`,
`risk` and `done`, and left the model to infer the rest from the raw JSON
schema. Plain function calling is best-effort about that, and on a page with 56
elements the model dropped the two fields that read like commentary.

The schema was right the whole time. This is the test for the other half: that
the prose asks for what the schema demands, so a field added to `Intent` cannot
silently become one the model is never told to send.
"""

import re

from src.agent.planner import SYSTEM_PROMPT, _missing_fields
from src.types import Intent


def test_the_prompt_names_every_required_field_of_intent():
    required = set(Intent.model_json_schema()["required"])

    unmentioned = {name for name in required if f"`{name}`" not in SYSTEM_PROMPT}

    assert not unmentioned, (
        f"the prompt never names {sorted(unmentioned)}, so the model is left to infer "
        "them from the schema alone -- which is how `reasoning` and `intent` came to be "
        "dropped on a real run"
    )


def test_the_prompt_says_the_two_prose_fields_are_required():
    # Naming them is not enough; they read like optional commentary unless the
    # prompt is explicit, which is precisely how the model treated them.
    assert "`reasoning`" in SYSTEM_PROMPT and "`intent`" in SYSTEM_PROMPT
    assert re.search(r"[Bb]oth are required", SYSTEM_PROMPT)


def test_missing_fields_extracts_the_names_from_a_real_pydantic_error():
    # The verbatim shape of the failure that prompted this.
    error = (
        "2 validation errors for Intent\n"
        "reasoning\n"
        "  Field required [type=missing, input_value={'action': 'type'}, input_type=dict]\n"
        "    For further information visit https://errors.pydantic.dev/2.13/v/missing\n"
        "intent\n"
        "  Field required [type=missing, input_value={'action': 'type'}, input_type=dict]\n"
        "    For further information visit https://errors.pydantic.dev/2.13/v/missing"
    )

    assert _missing_fields(error) == ["reasoning", "intent"]


def test_missing_fields_ignores_errors_that_are_not_about_missing_fields():
    error = (
        "1 validation error for Intent\n"
        "target_ref\n"
        "  Input should be a valid string [type=string_type, input_value=7, input_type=int]"
    )

    assert _missing_fields(error) == []


def test_missing_fields_is_empty_for_an_unparseable_message():
    assert _missing_fields("the model returned no tool call at all") == []


def test_the_prompt_requires_reading_a_value_before_declaring_done():
    """A goal that asks for a value has to be answered by a `read`, not a glance.

    Found by running against ParaBank with the goal "Find the total money in
    all accounts". The run "completed" in four steps: username, password, log
    in, done -- and read nothing. The planner was not wrong to do it. The
    prompt told it `done` needs "an expectation that proves it (for example,
    that the value you were asked to find is visible)", so it pointed a
    `text_visible` check at the $515.50 already on the page and stopped.

    Two things break downstream, and neither is the model's fault:

    - `src/artifact/compiler.py` derives an `OutputSpec` from `read` steps and
      from nothing else, so the compiled capability declared `outputs: []`. A
      capability whose entire purpose is to return a number returned nothing.
    - `_rewrite_checkpoint` swaps a literal in the `done` expectation for a
      structural claim only when a `read` step produced that literal. With no
      `read` there was nothing to swap, so the checkpoint stayed pinned to
      "$515.50" -- a balance on a public demo site that changes.

    So the prompt has to say that a value the goal asks for is `read` first and
    `done` second. This test holds that line, because the failure it prevents
    is silent: the run exits 0, the summary says "completed", and only the
    empty `outputs` list gives it away.
    """
    assert re.search(r"[Rr]ead", SYSTEM_PROMPT)
    assert re.search(
        r"`read`[^.]*before[^.]*`done`|`done`[^.]*only after[^.]*`read`",
        SYSTEM_PROMPT,
    ), (
        "the prompt never says a value the goal asks for must be captured with "
        "`read` before `done` -- without that, a run can satisfy the goal on "
        "screen, compile to a capability with no outputs, and still exit 0"
    )
