"""Every model that crosses a stage boundary in the discovery loop.

`extra="forbid"` throughout, so an unexpected field surfaces as a bug — it
matters most for `Intent`, parsed straight from an LLM tool call. Runtime
handles live on `exclude=True` fields, so the trace on disk stays pure data.
"""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Base(BaseModel):
    """Shared config for every model in the system."""

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #
class ActionKind(StrEnum):
    """What the planner may ask the surface to do.

    `READ` extracts a value without changing state. `DONE` declares the goal
    reached and carries no target.
    """

    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE = "type"
    SELECT = "select"
    READ = "read"
    WAIT = "wait"
    DONE = "done"


class RiskTier(StrEnum):
    """Risk tier. The planner self-assesses; policy re-derives and wins."""

    SAFE = "safe"                    # reversible, no side effects (navigate, read, type)
    CONSEQUENTIAL = "consequential"  # writes state but recoverable (submit a search)
    IRREVERSIBLE = "irreversible"    # money movement, deletion — Phase 1 halts on these


class StepOutcome(StrEnum):
    """How a single loop iteration ended."""

    OK = "ok"
    RETRIED = "retried"                    # same intent re-executed once
    REPLANNED = "replanned"               # planner asked again with last_failure
    VERIFY_FAILED = "verify_failed"       # expectation not met, recovery exhausted
    HALTED = "halted"                     # policy halt
    BUDGET_EXCEEDED = "budget_exceeded"   # ran out of steps or seconds


class RunOutcome(StrEnum):
    """How the whole discovery run ended."""

    COMPLETED = "completed"
    HALTED_POLICY = "halted_policy"
    BUDGET_EXCEEDED = "budget_exceeded"
    DEAD_END = "dead_end"        # planner produced no usable next step
    ERROR = "error"             # an unhandled exception; see Trace.error


# Only "structural" is wired in; the rest are declared for later.
PerceptionSource = Literal["structural", "vision", "ocr"]


# --------------------------------------------------------------------------- #
# Perception
# --------------------------------------------------------------------------- #
class BBox(Base):
    """Bounding box in CSS pixels, relative to the top-level viewport."""

    x: float
    y: float
    width: float
    height: float


class UIElement(Base):
    """One interactive control. Flat list; hierarchy is `parent_ref` only."""

    ref: str                       # snapshot-scoped id, e.g. "e1"; stable only within one UIState
    role: str                      # ARIA role from the a11y tree ("button", "textbox", ...)
    name: str                      # accessible name; may be synthesized from nearby text
    value: str | None = None       # current value for inputs/selects
    bbox: BBox | None = None       # screen box if resolvable, else None
    enabled: bool = True
    focused: bool = False
    parent_ref: str | None = None  # ref of the nearest ancestor element in this same list
    scope: str = "main"            # frame scope; "main" is the top document, else a frame key
    source: PerceptionSource = "structural"
    confidence: float = 1.0        # 1.0 for structural; lower once vision/OCR contribute

    # Opaque platform handle used by the executor to act on this element.
    # Excluded from serialization and repr so traces stay pure data.
    handle: Any = Field(default=None, exclude=True, repr=False)


class PerceptionCoverage(Base):
    """Result of the coverage check. On `ok is False`, the caller logs and
    continues — there is no vision fallback yet."""

    ok: bool
    reason: str | None = None          # human-readable failure cause, None when ok
    interactive_count: int             # number of interactive elements found
    unnamed_ratio: float               # fraction of elements with an empty accessible name
    empty_frames: list[str] = Field(default_factory=list)  # frame keys that yielded zero nodes


class UIState(Base):
    """One perceived snapshot: the "observe" output.

    `text_digest` is an order-stable summary of visible text, used by the loop
    to detect no-op steps.
    """

    url: str
    title: str
    elements: list[UIElement]
    text_digest: str
    coverage: PerceptionCoverage
    screenshot_path: str | None = None
    captured_at: datetime


# --------------------------------------------------------------------------- #
# Expectation — declared before acting, verified deterministically after
# --------------------------------------------------------------------------- #
class TextVisibleCheck(Base):
    """Pass if `text` appears in the post-action visible text."""

    kind: Literal["text_visible"] = "text_visible"
    text: str
    case_sensitive: bool = False


class TextNotVisibleCheck(Base):
    """Pass if `text` does NOT appear in the post-action visible text."""

    kind: Literal["text_not_visible"] = "text_not_visible"
    text: str
    case_sensitive: bool = False


class ElementVisibleCheck(Base):
    """Pass if an element matching the given role and/or name substring exists."""

    kind: Literal["element_visible"] = "element_visible"
    role: str | None = None
    name_contains: str | None = None


class UrlContainsCheck(Base):
    """Pass if the post-action URL contains `fragment`.

    `case_sensitive` defaults to False because no target distinguishes URLs by
    case. It exists, rather than being forbidden, because every other text check
    has it: planners kept supplying it here too, and rejecting a field that is
    reasonable to expect cost whole runs on a schema quibble. Honouring it is
    better than forbidding it.
    """

    kind: Literal["url_contains"] = "url_contains"
    fragment: str
    case_sensitive: bool = False


class FieldHasValueCheck(Base):
    """Pass if an element whose name contains `name_contains` has value
    `expected`. Used to confirm typed input landed."""

    kind: Literal["field_has_value"] = "field_has_value"
    name_contains: str
    expected: str
    case_sensitive: bool = False


# Discriminated union: the `kind` tag selects exactly one check shape.
ExpectationCheck = Annotated[
    TextVisibleCheck
    | TextNotVisibleCheck
    | ElementVisibleCheck
    | UrlContainsCheck
    | FieldHasValueCheck,
    Field(discriminator="kind"),
]


class Expectation(Base):
    """What the planner asserts will be true after its action runs.

    The verifier only ever evaluates `check`, never `description`.
    """

    description: str
    check: ExpectationCheck


# --------------------------------------------------------------------------- #
# Decide
# --------------------------------------------------------------------------- #
class Intent(Base):
    """The planner's decision for one step. Parsed from a strict tool call."""

    reasoning: str            # why this step, in one short paragraph
    intent: str               # short label of what the step accomplishes
    action: ActionKind
    target_ref: str | None = None       # ref from the CURRENT UIState; None for navigate/wait/done
    target_description: str = ""         # natural-language description of the target, for logs
    value: str | None = None            # text to type, option to select, or URL to navigate to
    risk: RiskTier
    expectation: Expectation


class Decision(Base):
    """`Intent` plus what it cost to produce. One per `StepRecord`."""

    intent: Intent
    model: str
    latency_ms: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    raw_response_id: str | None = None


# --------------------------------------------------------------------------- #
# Guard (policy)
# --------------------------------------------------------------------------- #
class PolicyVerdict(Base):
    """Output of the guard stage. Policy is code, never prompt text."""

    allowed: bool
    tier: RiskTier                 # policy's own risk assessment (may differ from Intent.risk)
    rule: str                     # id of the rule that decided, e.g. "origin_allowlist"
    reason: str                   # human-readable explanation
    disposition: Literal["allow", "halt"]   # Phase 1 has only these two outcomes


# --------------------------------------------------------------------------- #
# Act + settle
# --------------------------------------------------------------------------- #
class ResolvedTarget(Base):
    """A `target_ref` turned back into a concrete element and a live handle,
    ready for the executor. Phase 1 resolves by ref only; `resolved_by` is the
    seam for description-based re-resolution later."""

    ref: str
    element: UIElement
    handle: Any = Field(default=None, exclude=True, repr=False)
    resolved_by: Literal["ref", "description"] = "ref"


class SettleResult(Base):
    """Result of waiting for the UI to stop changing after an action."""

    settled: bool
    waited_ms: int
    signal: str        # what ended the wait: "network_idle", "dom_stable", "timeout"


class ActResult(Base):
    """Outcome of executing one `Intent` against the surface."""

    ok: bool
    action: ActionKind
    duration_ms: int
    error: str | None = None
    read_value: str | None = None        # populated only for READ actions
    settle: SettleResult | None = None   # populated once the settle stage runs


# --------------------------------------------------------------------------- #
# Verify
# --------------------------------------------------------------------------- #
class VerifyResult(Base):
    """Evaluation of `Expectation.check` against the post-action state."""

    ok: bool
    expectation: Expectation
    check_performed: str   # concrete description of what was evaluated
    evidence: str          # what was actually observed


# --------------------------------------------------------------------------- #
# Trace
# --------------------------------------------------------------------------- #
class StepRecord(Base):
    """One fully-resolved loop iteration. One of these per line in trace.jsonl."""

    index: int
    started_at: datetime
    duration_ms: int
    state_before: UIState
    decision: Decision
    policy: PolicyVerdict
    act: ActResult | None = None       # None if policy halted before acting
    verify: VerifyResult | None = None  # None if the action never ran
    outcome: StepOutcome
    recovery_note: str | None = None   # set when this step was a retry or re-plan
    screenshot_path: str | None = None


class Trace(Base):
    """The full record of one discovery run. `run.json` is this object without
    `steps`; `trace.jsonl` is the `steps` list, one per line."""

    run_id: str
    goal: str
    target_url: str
    started_at: datetime
    finished_at: datetime | None = None
    outcome: RunOutcome | None = None
    steps: list[StepRecord] = Field(default_factory=list)
    summary: str | None = None
    error: str | None = None
