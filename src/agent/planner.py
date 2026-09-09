"""The one LLM call per step.

Given the current screen and the goal, produce the next `Intent` and the
`Expectation` that will judge it. Allow/deny is `Policy.check`; pass/fail is
`verify_expectation`. Neither happens here.

The tool schema is generated from `Intent`, so it cannot drift from
`src/types.py`.
"""

from __future__ import annotations

import base64
import logging
import time
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from src.llm import apply_sampling, is_reasoning_model
from src.types import Decision, Intent, UIState

logger = logging.getLogger(__name__)

# Re-exported: the rule now lives in `src.llm` because the semantic pass
# needs it too, and duplicating it there is what left that pass 400ing in
# silence for every reasoning model.
_is_reasoning_model = is_reasoning_model

TOOL_NAME = "propose_step"
CHAT_COMPLETIONS_PATH = "/chat/completions"
DEFAULT_REQUEST_TIMEOUT_S = 60.0

SYSTEM_PROMPT = """\
You are the planning stage of a computer-use agent. You do not act on the UI
yourself; you choose the single next action for an executor to perform.

You will be shown, for the current page: its URL and title, a flat table of
every element perception could see, the steps already taken this run, and --
if the previous attempt failed -- what went wrong.

Rules:
- Call the propose_step tool exactly once, with exactly one next action. Never
  describe more than one step.
- Every call must include `reasoning` (a short paragraph on why this step, given
  the page and the goal) and `intent` (a few words labelling what it does, e.g.
  "type the member id"). Both are required. They are not commentary: they are
  what a person reads in the trace afterwards to judge whether the run was
  sensible, and a step without them is a step nobody can review. Omitting either
  makes the whole call invalid and it will be rejected.
- `action` is what the executor will do: one of navigate, click, type, select,
  read, wait, done.
- `target_ref` must be a ref from the CURRENT table (e.g. "e7"). Leave it
  unset for `navigate`, `wait`, and `done`, which act on no element.
- A ref with no handle is read-only text, perceived directly from the page.
  You may `read` it but cannot `click` or `type` into it.
- Every action must declare an `expectation`, and it has exactly two fields:
  `description` (prose, for a human) and `check` (the machine-checked part).
  The check kind and its arguments go *inside* `check`, never alongside it:

      "expectation": {
        "description": "the accounts overview page loads",
        "check": {"kind": "url_contains", "fragment": "overview.htm"}
      }

  Choose the check kind that actually matches what should become true --
  visible text appearing or disappearing (`text_visible`, `text_not_visible`),
  an element appearing (`element_visible`), the URL changing (`url_contains`),
  or a field holding a value you typed (`field_has_value`). The check is
  evaluated mechanically, never judged by a model.
- Never use a `field_has_value` check on a password or passcode field. The
  browser masks those values and perception will not report them, so such a
  check can never pass however well the typing worked. Verify credential entry
  by what happens next instead -- the URL changing, or an element that only
  appears once signed in.
- When the goal asks you to find, read, check or report a value, capture it
  with a `read` action on the element that holds it, *before* `done`. Seeing
  the value on screen is not the same as retrieving it: a `read` is the only
  action that returns anything to the caller, so a run that ends on `done`
  alone answers the goal for whoever is watching the browser and for nobody
  else. If the goal names several values, `read` each one.
- Use `done` only once the goal is fully satisfied -- including that read --
  with an expectation that proves it. Prefer a check that stays true when the
  underlying data changes: that you are on the page the answer lives on, or
  that the element holding it is present. A check pinned to the exact figure
  you happened to see is a check that fails the next time the figure moves.
- `risk` is your own honest rough assessment (safe / consequential /
  irreversible). A separate safety system re-derives this independently and
  can halt the run regardless of what you pick, so there is no benefit to
  understating it.
- Do not reason about permissions or safety policy -- that is enforced after
  you decide. Focus only on the next useful action toward the goal.
"""



class PlannerError(Exception):
    """The planner could not produce a valid `Intent`.

    Uncaught here; `run_discovery` turns it into `RunOutcome.ERROR`.
    """


class Planner:
    """Calls the configured model once per `decide()`."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        fallback_models: list[str] | None = None,
        enable_model_routing: bool = True,
        username: str | None = None,
        password: str | None = None,
        request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._model = model
        # OpenRouter retries these on the same request; survives a 429.
        self._fallback_models = list(fallback_models or [])
        self._enable_model_routing = enable_model_routing
        self._username = username
        self._password = password
        # Explicit join: httpx's base_url would drop the /api/v1 path.
        self._chat_completions_url = f"{base_url.rstrip('/')}{CHAT_COMPLETIONS_PATH}"
        self._client = http_client or httpx.Client(
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=request_timeout_s,
        )
        self._tools = [_build_tool_schema()]

    def close(self) -> None:
        """Release the HTTP client. Owned by `cli.py`, not the loop."""
        self._client.close()

    def decide(
        self,
        goal: str,
        state: UIState,
        history: list[str],
        last_failure: str | None,
    ) -> Decision:
        """Next `Intent`, with one corrective retry on an invalid response.

        `latency_ms` covers the retry too.
        """
        started = time.monotonic()

        messages = self._build_messages(goal, state, history, last_failure, correction=None)
        response_json = self._call_model(messages)

        try:
            intent = self._extract_intent(response_json)
        except (ValidationError, PlannerError) as first_error:
            logger.warning(
                "planner: response was not a valid Intent (%s); retrying once with correction",
                first_error,
            )
            messages = self._build_messages(
                goal, state, history, last_failure, correction=str(first_error)
            )
            response_json = self._call_model(messages)
            try:
                intent = self._extract_intent(response_json)
            except (ValidationError, PlannerError) as second_error:
                raise PlannerError(
                    "planner could not produce a schema-valid Intent even after one "
                    f"corrective retry: {second_error}"
                ) from second_error

        usage = response_json.get("usage") or {}
        return Decision(
            intent=intent,
            # A fallback may have answered; record which one actually did.
            model=response_json.get("model") or self._model,
            latency_ms=int((time.monotonic() - started) * 1000),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            raw_response_id=response_json.get("id"),
        )

    # -- request/response plumbing ------------------------------------------ #

    def _call_model(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "tools": self._tools,
            "tool_choice": {"type": "function", "function": {"name": TOOL_NAME}},
        }
        apply_sampling(payload, self._model)
        # OpenRouter only. OpenAI 400s on unrecognised body params.
        if self._fallback_models and self._enable_model_routing:
            payload["models"] = [self._model, *self._fallback_models]
        try:
            response = self._client.post(self._chat_completions_url, json=payload)
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise PlannerError(f"LLM request failed: {error}") from error
        return response.json()

    def _extract_intent(self, response_json: dict[str, Any]) -> Intent:
        """Validate the forced tool call's arguments as an `Intent`.

        `ValidationError` means the model answered wrongly; `PlannerError`
        means the response was not shaped like a tool call at all.
        """
        try:
            message = response_json["choices"][0]["message"]
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                raise PlannerError(
                    "model did not call propose_step despite a forced tool_choice; "
                    f"content was {message.get('content')!r}"
                )
            arguments = tool_calls[0]["function"]["arguments"]
        except (KeyError, IndexError, TypeError) as error:
            raise PlannerError(f"unexpected LLM response shape: {error}") from error
        return Intent.model_validate_json(arguments)

    # -- prompt construction -------------------------------------------------- #

    def _build_messages(
        self,
        goal: str,
        state: UIState,
        history: list[str],
        last_failure: str | None,
        correction: str | None,
    ) -> list[dict[str, Any]]:
        user_content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": self._render_user_prompt(goal, state, history, last_failure, correction),
            }
        ]
        # Decision: attach the screenshot only when structural perception is
        # degraded or the last attempt failed -- everything else is a plain
        # text-only call, which keeps the common case cheap.
        if not state.coverage.ok or last_failure is not None:
            image_part = _screenshot_content_part(state.screenshot_path)
            if image_part is not None:
                user_content.append(image_part)

        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

    def _render_user_prompt(
        self,
        goal: str,
        state: UIState,
        history: list[str],
        last_failure: str | None,
        correction: str | None,
    ) -> str:
        lines = [
            f"Goal: {goal}",
            "",
            _render_credentials(self._username, self._password),
            "",
            "Steps taken so far:",
            _render_history(history),
            "",
        ]
        if last_failure:
            lines.append(f"The previous attempt did not succeed: {last_failure}")
            lines.append("")
        lines += [
            "Current page:",
            f"URL: {state.url}",
            f"Title: {state.title}",
            "",
            _render_state_table(state),
            "",
        ]
        if correction:
            lines.append(
                "Your previous tool call could not be parsed as valid propose_step "
                f"arguments: {correction}"
            )
            missing = _missing_fields(correction)
            if missing:
                # The raw pydantic error buries the field names in several lines
                # of type noise, and the model demonstrably reads past them --
                # `reasoning` and `intent` were the two it kept dropping. Say the
                # names on their own line.
                lines.append(
                    "You omitted these required fields: "
                    + ", ".join(missing)
                    + ". Include every one of them this time."
                )
            lines.append("Call propose_step again with corrected, schema-valid arguments.")
        else:
            lines.append(f"Call {TOOL_NAME} with the single next action to take.")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Pure helpers -- no instance state, easy to reason about in isolation.
# --------------------------------------------------------------------------- #
def _missing_fields(validation_error: str) -> list[str]:
    """Field names a pydantic error reported as missing, in the order given.

    Pydantic renders these as the field name on its own line followed by an
    indented "Field required" line, so the pairing is what identifies them --
    matching the message alone would also catch fields mentioned for other
    reasons.
    """
    names: list[str] = []
    lines = validation_error.splitlines()
    for index, line in enumerate(lines[:-1]):
        candidate = line.strip()
        if not candidate or " " in candidate:
            continue
        if lines[index + 1].strip().startswith("Field required") and candidate not in names:
            names.append(candidate)
    return names


def _render_credentials(username: str | None, password: str | None) -> str:
    if username is None and password is None:
        return "Available credentials for this target: none provided."
    return (
        "Available credentials for this target (use only if the UI asks for them):\n"
        f"  username: {username or '(not provided)'}\n"
        f"  password: {password or '(not provided)'}"
    )


def _render_history(history: list[str]) -> str:
    if not history:
        return "(none yet -- this is the first step)"
    return "\n".join(history)


def _render_state_table(state: UIState) -> str:
    """The element table, decision 7's flat-list discipline as a prompt.

    Markdown-style so the model reads it as a table, but not column-aligned --
    aligning would mean walking the data twice to measure widths for a
    formatting nicety the model does not need.
    """
    rows = [
        "ref | role | name | value | enabled | scope | parent",
        "--- | --- | --- | --- | --- | --- | ---",
    ]
    for element in state.elements:
        rows.append(
            " | ".join(
                [
                    _cell(element.ref),
                    _cell(element.role),
                    _cell(element.name),
                    _cell(element.value or ""),
                    "true" if element.enabled else "false",
                    _cell(element.scope),
                    _cell(element.parent_ref or ""),
                ]
            )
        )
    return "\n".join(rows)


def _cell(text: str) -> str:
    """Normalize one table cell so a stray pipe or newline in page text can
    never be mistaken for a column break."""
    return text.replace("|", "/").replace("\n", " ")


def _screenshot_content_part(screenshot_path: str | None) -> dict[str, Any] | None:
    """An OpenAI-format image content-part for the step's screenshot, or
    `None` when there is nothing to attach."""
    if not screenshot_path:
        return None
    path = Path(screenshot_path)
    if not path.exists():
        return None
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}}


def _build_tool_schema() -> dict[str, Any]:
    """The `propose_step` tool definition, generated from `Intent` itself.

    Deriving rather than hand-writing means there is exactly one place that
    defines a valid step -- `src/types.py` -- and this schema cannot drift out
    of sync with it. `extra="forbid"` on every model in that file is also what
    makes Pydantic emit `additionalProperties: false` here for free.
    """
    return {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": (
                "Propose the single next action to take toward the goal, given "
                "the current perceived UI state."
            ),
            "parameters": Intent.model_json_schema(),
        },
    }
