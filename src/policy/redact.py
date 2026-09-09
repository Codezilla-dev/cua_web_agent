"""Keep secrets out of anything that reaches disk.

Two mechanisms, because one is not enough.

**By target name.** If a step typed into a field whose accessible name looks
sensitive (`password`, `pin`, `ssn`, ...), its `intent.value` is replaced. This
catches a secret we can recognise by *where it was going*, without needing to
know the value.

**By value.** Every secret the run was given is scrubbed from every string in
the record, wherever it ended up. This exists because the first mechanism is
not sufficient, and the evidence proved it: scrubbing `intent.value` alone left
the same password readable in seven other places --

    decision.intent.reasoning                    the model quoting it back
    decision.intent.expectation.description      the model's own prose
    decision.intent.expectation.check.expected   a structured field_has_value
    verify.expectation.*                         the same expectation, echoed
    verify.check_performed                       the rendered check description
    state_before.elements[].value                read straight back off the DOM
    state_before.elements[].name                 ditto, as a synthesised name

A secret spreads into model prose, declared expectations, and perceived element
values. Any scheme that scrubs one named field will always be a step behind it.
So the value sweep walks the whole serialised record and is the mechanism we
actually rely on; the name rule is the backstop for a value we were never told.

Replacement is case-sensitive and literal. If the password is `operator`, the
label `Operator ID` is deliberately left alone: we scrub the secret as it was
typed, not every word that resembles it. A credential equal to a common UI
string is a property of demo fixtures, not of the design.

Copy-on-write: `loop.py` keeps the originals in `Trace.steps`, so only the copy
handed to the evidence writer is scrubbed.
"""

import re
from collections.abc import Iterable
from typing import Any

from src.policy._targets import resolve_target_name
from src.types import StepRecord


class Redactor:
    """Scrubs secrets out of a step before it is persisted."""

    def __init__(
        self,
        sensitive_name_pattern: str,
        replacement: str,
        secret_values: Iterable[str] = (),
    ) -> None:
        self._pattern = re.compile(sensitive_name_pattern)
        self._replacement = replacement
        # Longest first, so a secret containing another is replaced whole rather
        # than being half-eaten by the shorter one.
        self._secrets = sorted(
            {value for value in secret_values if value}, key=len, reverse=True
        )

    def redact_step(self, step: StepRecord) -> StepRecord:
        """Return a copy of `step` safe to write to disk."""
        step = self._redact_sensitive_target_value(step)
        return self._sweep_secret_values(step)

    def scrub(self, value: Any) -> Any:
        """Sweep known secrets out of any JSON-able structure.

        `redact_step` is the discovery loop's entry point and is shaped around
        `StepRecord`. Replay writes a different record type for a good reason
        (there is no planner `Decision` behind a replay step), but it needs the
        same value sweep, so the sweep is exposed rather than reimplemented.
        The name-pattern rule stays private to `redact_step`: it depends on a
        resolved target name, which only a discovery step has.
        """
        return self._scrub(value)

    def _redact_sensitive_target_value(self, step: StepRecord) -> StepRecord:
        """Scrub the value at a sensitive-looking field, in or out.

        Matches the target's accessible name or the planner's own description of
        it; either is reason enough.

        Both directions matter. `intent.value` is what was typed *into* the
        field, and was the only thing scrubbed here originally -- but a `read`
        puts what it took *out* of the field into `act.read_value`, and that
        path wrote the raw value straight to `trace.jsonl`. Typing a token into
        a field named "API key" was redacted; reading the same token back out
        of it was not.
        """
        intent = step.decision.intent
        target_name = resolve_target_name(intent.target_ref, step.state_before.elements)

        matched = self._pattern.search(target_name) or self._pattern.search(
            intent.target_description
        )
        if not matched:
            return step

        redacted_intent = intent.model_copy(update={"value": self._replacement})
        redacted_decision = step.decision.model_copy(update={"intent": redacted_intent})
        update: dict[str, Any] = {"decision": redacted_decision}
        if step.act is not None and step.act.read_value:
            update["act"] = step.act.model_copy(update={"read_value": self._replacement})
        return step.model_copy(update=update)

    def _sweep_secret_values(self, step: StepRecord) -> StepRecord:
        """Replace every known secret in every string anywhere in the record."""
        if not self._secrets:
            return step
        scrubbed = self._scrub(step.model_dump())
        return StepRecord.model_validate(scrubbed)

    def _scrub(self, value: Any) -> Any:
        """Walk the structure, rewriting strings and leaving other types alone."""
        if isinstance(value, str):
            return self._scrub_text(value)
        if isinstance(value, dict):
            return {key: self._scrub(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._scrub(item) for item in value]
        return value

    def _scrub_text(self, text: str) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, self._replacement)
        return text
