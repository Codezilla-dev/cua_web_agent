"""What a replay leaves on disk.

A discovery run and a replay produce different records, and the difference is
not cosmetic. A `StepRecord` is built around a planner `Decision` -- reasoning,
a proposed intent, a declared expectation. A replay step has no decision: the
capability already said what to do, and the only interesting facts are whether
the target resolved, whether the action ran, and whether the expectation held.
Reusing `StepRecord` here would mean inventing a `Decision` that no model
produced, which would put fiction into the evidence bundle. So replay gets its
own record, deliberately smaller.

Redaction is not optional. Replay binds real credentials into real actions --
that is the whole point of `CredentialRef` -- so every string written here goes
through the same secret sweep the discovery loop uses. The writer takes a
`Redactor` as a required argument for the same reason `EvidenceWriter` does:
so no caller can construct one that writes raw values by accident.

**Credentials and personal data are two different problems.** A credential is
the same string on every run and the run was told what it is, so it can be
swept by value. A member id is different on every invocation and is only
sensitive because the capability's author says it is -- so it is masked by
*declaration*: `ParamSpec.sensitivity == "pii"` means the caller's value for
that parameter is masked wherever it appears in this bundle. The two passes
are kept separate and use different markers, because a reader of a trace
should be able to tell 'a password was here' from 'an identifier was here'.
"""

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from src.policy.redact import Redactor
from src.replay.outcomes import ReplayResult
from src.types import ActionKind

# Distinct from the credential replacement on purpose: a reader of a trace
# should be able to tell "a password was here" from "an identifier was here".
PII_MASK = "***PII***"


class ReplayStepRecord(BaseModel):
    """One replayed step. One of these per line in `trace.jsonl`."""

    model_config = ConfigDict(extra="forbid")

    index: int
    intent: str
    action: ActionKind

    # The target as the capability described it, flattened so a reader can scan
    # the trace without cross-referencing the artifact.
    target_role: str | None = None
    target_name: str | None = None
    target_scope: str | None = None
    target_match_index: int | None = None

    # Which kind of value went in -- `param`, `credential`, `constant`, or None.
    # Never the value itself for a credential; the sweep would catch it, but not
    # writing it is better than scrubbing it.
    value_kind: str | None = None

    resolved: bool | None = None
    # Which rung of the locator chain found the target: `anchor`, `exact`,
    # `normalised` or `contains`. A capability that has quietly slid to
    # `contains` still works and is drifting, and this is where that shows.
    strategy_used: str | None = None
    policy_disposition: str | None = None
    policy_rule: str | None = None
    act_ok: bool | None = None
    act_error: str | None = None
    read_value: str | None = None
    verify_ok: bool | None = None
    check_performed: str | None = None
    observed: str | None = None
    business_outcome: str | None = None
    # Acting steps in a row that changed nothing, counted at this step. A run
    # whose trace shows this climbing is going nowhere while every individual
    # step still reports success -- the pattern no step-level check can see.
    no_progress_steps: int = 0
    recovery_note: str | None = None
    duration_ms: int = 0
    screenshot_path: str | None = None


class ReplayEvidenceWriter:
    """Writes one replay's evidence under `<root>/<run_id>/`.

    Mirrors `EvidenceWriter`'s shape on purpose: same directory layout, same
    streamed `trace.jsonl`, same per-step PNG naming. A reviewer who has read a
    discovery bundle can read a replay bundle without being told how.
    """

    def __init__(
        self,
        run_id: str,
        redactor: Redactor,
        root: str | Path = "evidence",
        masked_values: Iterable[str] = (),
    ) -> None:
        self.run_id = run_id
        self._redactor = redactor
        # Longest first, for the same reason the credential sweep sorts: a value
        # containing another must be replaced whole rather than half-eaten.
        self._masked = sorted(
            {value for value in masked_values if value}, key=len, reverse=True
        )
        self._dir = Path(root) / run_id
        self._dir.mkdir(parents=True, exist_ok=True)
        self._trace_path = self._dir / "trace.jsonl"

    @property
    def evidence_dir(self) -> Path:
        return self._dir

    def step_png_path(self, index: int) -> str:
        """Where step `index`'s screenshot belongs, e.g. `.../step_02.png`."""
        return str(self._dir / f"step_{index:02d}.png")

    def append_step(self, record: ReplayStepRecord) -> None:
        """Scrub `record` and append it to `trace.jsonl`.

        Reopened per call so each line is durable if the replay later crashes.
        """
        scrubbed = self._scrubbed(record.model_dump(mode="json"))
        with self._trace_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(scrubbed) + "\n")

    def write_result(
        self, result: ReplayResult, capability_id: str, params: dict[str, str]
    ) -> None:
        """Write `result.json`: the invocation and its typed outcome.

        The result's fields are flattened to the top level with `status`
        rendered as `outcome`, so the file answers "what happened" on its first
        line rather than behind a wrapper key. `params` is included because a
        result is not interpretable without the inputs that produced it -- and
        is swept, since a caller can pass anything through it.
        """
        payload: dict[str, Any] = {
            "capability_id": capability_id,
            "params": params,
            "outcome": result.status,
        }
        payload.update(result.model_dump(mode="json", exclude={"status"}))

        scrubbed = self._scrubbed(payload)
        path = self._dir / "result.json"
        path.write_text(json.dumps(scrubbed, indent=2) + "\n", encoding="utf-8")

    def _scrubbed(self, payload: Any) -> Any:
        return self._mask(self._redactor.scrub(payload))

    def _mask(self, value: Any) -> Any:
        """Replace declared-`pii` parameter values wherever they appear.

        Runs after the credential sweep and over the whole structure, not over
        the `params` dict alone: a member id reaches the trace through the typed
        value, the bound expectation, the observed URL and the rendered check
        description. Masking only where it was passed in would leave it readable
        in four other places -- the same lesson the credential sweep learned.
        """
        if not self._masked:
            return value
        if isinstance(value, str):
            for secret in self._masked:
                value = value.replace(secret, PII_MASK)
            return value
        if isinstance(value, dict):
            return {key: self._mask(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._mask(item) for item in value]
        return value
