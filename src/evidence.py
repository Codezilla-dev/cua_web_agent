"""What a discovery run leaves on disk: `trace.jsonl`, `run.json`, `summary.md`,
step PNGs.

Steps are streamed as they finish, not buffered. Redaction happens here, so
only what lands on disk is scrubbed -- `run_discovery`'s return value keeps
the real values.
"""

import secrets
from datetime import UTC, datetime
from pathlib import Path

from src.policy.redact import Redactor
from src.summary import one_line as summarise
from src.summary import render as render_summary
from src.types import StepRecord, Trace


def generate_run_id() -> str:
    """`YYYYmmddTHHMMSSZ-<4 hex>`: sortable, and unique within a second."""
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    suffix = secrets.token_hex(2)
    return f"{timestamp}-{suffix}"


class EvidenceWriter:
    """Writes one run's evidence under `<root>/<run_id>/`.

    `redactor` is required so no writer can persist raw values by accident.
    """

    def __init__(self, run_id: str, redactor: Redactor, root: str | Path = "evidence") -> None:
        self.run_id = run_id
        self._redactor = redactor
        self._dir = Path(root) / run_id
        self._dir.mkdir(parents=True, exist_ok=True)
        self._trace_path = self._dir / "trace.jsonl"

    @property
    def evidence_dir(self) -> Path:
        """Where this run's files live. For `cli.py`'s summary line."""
        return self._dir

    def step_png_path(self, index: int) -> str:
        """Where step `index`'s screenshot belongs, e.g. `.../step_02.png`."""
        return str(self._dir / f"step_{index:02d}.png")

    def append_step(self, step: StepRecord) -> StepRecord:
        """Redact `step`, append it to `trace.jsonl`, and hand the copy back.

        Reopened per call so each line is durable if the run later crashes.

        The redacted copy is returned because the loop needs it too: the
        planner's history is rendered from it, and redacting one copy for disk
        while rendering another for the prompt is how a scrubbed value gets
        put back in front of the model.
        """
        redacted_step = self._redactor.redact_step(step)
        with self._trace_path.open("a", encoding="utf-8") as handle:
            handle.write(redacted_step.model_dump_json() + "\n")
        return redacted_step

    def write_run(self, trace: Trace) -> None:
        """Write `run.json` and the human-readable `summary.md`.

        `run.json` is the trace without its `steps`; `summary.md` is the same
        run rendered as prose for a person who wants to know what happened
        rather than audit it.

        Both are swept. `run.json` used not to need it -- excluding `steps` left
        nothing with a typed value in it -- but `Trace.summary` is *derived*
        from the steps, so that reasoning no longer holds. `summary.py` already
        declines to print a value typed into a credential field; this is the
        backstop, and the sweep is the mechanism this project relies on.
        """
        trace = trace.model_copy(update={"summary": summarise(trace)})

        run_path = self._dir / "run.json"
        run_json = trace.model_dump_json(exclude={"steps"}, indent=2)
        run_path.write_text(self._scrub_text(run_json), encoding="utf-8")

        summary_path = self._dir / "summary.md"
        summary_path.write_text(self._scrub_text(render_summary(trace)), encoding="utf-8")

    def _scrub_text(self, text: str) -> str:
        scrubbed = self._redactor.scrub(text)
        return scrubbed if isinstance(scrubbed, str) else text
