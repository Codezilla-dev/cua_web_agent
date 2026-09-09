"""The Capability: a recorded flow, typed and replayable without a model.

A `Trace` is a record of what happened once. A `Capability` is a contract for
doing it again — the same flow with different inputs, executed by code rather
than by a planner. The difference drives every design choice here.

**Targets are described, not referenced.** A trace step points at `e7`, a ref
that is only meaningful inside the snapshot that produced it. A capability step
carries a `TargetSpec` — role, accessible name, frame scope, and which match to
take when there are several. That is the same information perception publishes,
so replay resolves a target by looking for it, not by remembering a number.
There is no CSS and no XPath here, deliberately: the whole system perceives
through the accessibility tree, and a locator strategy that reaches past it
would be the one thing in the pipeline that a legacy DOM could break.

**Values are parameters or constants, never credentials.** A recorded run typed
a literal member id; a capability takes it as a typed input. Credentials are
neither — they are configuration, resolved at replay from the environment, and
`CredentialRef` exists so that an artifact can say "the password goes here"
without ever containing one.

**Only the successful path is recorded.** The trace keeps retries and replans
because they are evidence. The capability drops them: replay should perform the
steps that worked, not re-enact the discovery.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.types import ActionKind, Expectation, RiskTier

# Bumped when a change to these models makes an older artifact unreadable.
# Replay refuses a version it does not know rather than guessing.
SCHEMA_VERSION = 1


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AnchorSpec(Base):
    """Find a control by its position next to a stable neighbour.

    Some values have no name of their own. On the target app the savings balance
    is a bare table cell -- the accessibility tree reports it as `StaticText`
    whose accessible name *is* the number, because there is nothing else to call
    it. Recording that name produces a target that can only ever match the
    recorded record: `$4,812.55` is not where the balance is, it is what the
    balance was.

    The structural fact is the neighbour. The balance sits in the same row as the
    account type, so "the element just after the one called 'Savings'" survives a
    change of member while "the element called '$4,812.55'" does not. This is the
    label-anchored strategy, and it exists because a11y-tree perception yields a
    flat ordered list -- position relative to a named sibling is the one
    structural relationship it reliably preserves.

    Deliberately not a CSS selector or an XPath. Those would reach past the
    accessibility tree, which is the one thing the whole design refuses to do.
    """

    # The nearest preceding element whose name is stable across records.
    after_name: str
    # How many elements past the anchor the target sits. Recorded, not assumed.
    offset: int = 1
    # What the target should be when found there; a guard against the page
    # growing a row and the offset landing on something else entirely.
    role: str | None = None


class TargetSpec(Base):
    """How to find a control again, in terms perception actually publishes.

    `match_index` is the honest part: when a page has three links named
    "Open Record", the recording knows which one it used, and replay must take
    the same one rather than the first. It is recorded even when there was only
    one match, so a page that later grows a second one fails loudly instead of
    silently picking differently.
    """

    role: str
    name: str
    scope: str = "main"
    match_index: int = 0
    # What the planner called it. Never used for matching -- it is here so a
    # human reviewing the artifact can tell what the step was for.
    described_as: str = ""
    # Set when `name` is the recorded *value* rather than a stable identifier,
    # which is how a bare table cell perceives. When present, replay prefers it:
    # the anchor is the part that survives a different record.
    anchor: "AnchorSpec | None" = None


class CredentialRef(Base):
    """A placeholder for a secret supplied at replay time, never stored."""

    kind: Literal["credential"] = "credential"
    # Which credential: matches the names `config.target` exposes.
    field: Literal["username", "password"]


class ParamRef(Base):
    """A placeholder for a typed input supplied by the caller."""

    kind: Literal["param"] = "param"
    name: str


class Constant(Base):
    """A literal recorded from the run, replayed unchanged."""

    kind: Literal["constant"] = "constant"
    value: str


# What goes into a step's `value` slot. Discriminated so an artifact can be read
# without guessing whether a string is a secret, an input, or a fixed value.
StepValue = ParamRef | CredentialRef | Constant


class ParamSpec(Base):
    """One input the caller must supply to invoke this capability."""

    name: str
    type: Literal["string"] = "string"
    required: bool = True
    description: str = ""
    # The value used during the recording. Kept as documentation and as the
    # default for a smoke replay; never a secret, by construction.
    example: str = ""
    # How the caller's value for this parameter should be treated on disk.
    #
    # `public` is the default and writes the value as-is -- a member id in a
    # trace is what makes the trace readable. `pii` masks it in the replay
    # evidence: the run is still auditable (you can see a lookup happened and
    # what it returned) without the bundle carrying the identifier around.
    # `secret` is declared for completeness and is not what credentials use --
    # those are `CredentialRef`, and are never values in the artifact at all.
    #
    # Nothing infers this. It is a declaration on the artifact, set by whoever
    # reviews the capability, because "is this string personal data" is a
    # question about the tenant's data, not about the flow. Defaulting it to
    # `pii` would be safer-sounding and would mask every member id in every
    # trace, which is the state the evidence is least useful in.
    sensitivity: Literal["public", "pii", "secret"] = "public"


class OutputSpec(Base):
    """One value this capability returns to its caller."""

    name: str
    type: Literal["string"] = "string"
    description: str = ""
    # Index of the step whose `read` produced it.
    source_step: int


class CapabilityStep(Base):
    """One action, with everything replay needs and nothing it does not."""

    index: int
    # The planner's own description, kept for review. Replay ignores it.
    intent: str
    action: ActionKind
    target: TargetSpec | None = None
    value: StepValue | None = Field(default=None, discriminator="kind")
    risk: RiskTier
    # Asserted after the action, by the same verifier the discovery loop used.
    expectation: Expectation


class SurfaceRef(Base):
    """Where this capability runs. Kept tenant-neutral: an entry path, not a host.

    Splitting the origin out is what would let the same recording run against a
    second institution's deployment of the same vendor app -- the flow is the
    same, only the host differs.
    """

    kind: Literal["web"] = "web"
    entry_path: str
    recorded_origin: str


class Provenance(Base):
    """Where this capability came from. Enough to audit it, not the transcript."""

    discovery_run_id: str
    goal: str
    model: str
    recorded_at: datetime
    # Steps in the originating run, including the ones that failed. A capability
    # compiled from a run that needed six retries deserves a closer look than
    # one that went straight through.
    source_step_count: int
    retried_step_count: int


class Capability(Base):
    """A reusable, reviewable, replayable flow.

    Serialised as canonical JSON. A reviewer should be able to read the file and
    answer three questions without opening the code: what does this do, what
    does it need, and what does it give back.
    """

    schema_version: int = SCHEMA_VERSION
    id: str
    version: int = 1
    title: str
    description: str = ""
    surface: SurfaceRef
    inputs: list[ParamSpec] = Field(default_factory=list)
    outputs: list[OutputSpec] = Field(default_factory=list)
    steps: list[CapabilityStep]
    # The condition that proves the goal was reached, taken from the `done` step.
    # Replay asserts this before reporting success, so a flow that walked every
    # step but landed somewhere wrong is a failure, not a pass.
    checkpoint: Expectation
    provenance: Provenance
