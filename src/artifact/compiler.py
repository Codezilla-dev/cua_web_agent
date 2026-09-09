"""Turn a successful discovery `Trace` into a replayable `Capability`.

No model is involved. Everything here is a rule you can read and predict, which
matters because the compiler decides what a capability *means* — which values
become parameters, which are secrets, and which steps are worth keeping. If that
were a model's judgement, two compilations of the same trace could disagree.

Four rules do the work:

1. **Keep only steps that worked.** A trace records retries and replans as
   evidence. Replay should perform the flow that succeeded, not re-enact the
   discovery, so failed attempts are dropped and the survivors renumbered.

2. **A value typed into a credential field becomes a `CredentialRef`.** It is
   never stored, not even redacted — the artifact says where the password goes
   and replay fetches it from config.

3. **A value that appears in the goal becomes a parameter.** "look up member
   12345" plus a step that typed `12345` is the signature of an input: the
   number is in the request, not in the flow. Anything else typed is a constant.
   This is a heuristic and is documented as one; the alternative is asking a
   model, which would make compilation non-deterministic.

4. **A `read` step becomes an output.** That is the only action whose purpose is
   to produce a value for the caller.

Names for parameters and outputs are slugged from the field the value went into,
or from the planner's description of what it read. They are derived rather than
invented so that compiling the same trace twice gives the same contract.
"""

import re
from collections.abc import Mapping

from src.agent.verify import is_unobservable_field
from src.artifact.models import (
    SCHEMA_VERSION,
    AnchorSpec,
    Capability,
    CapabilityStep,
    Constant,
    CredentialRef,
    OutputSpec,
    ParamRef,
    ParamSpec,
    Provenance,
    StepValue,
    SurfaceRef,
    TargetSpec,
)
from src.types import (
    ActionKind,
    ElementVisibleCheck,
    Expectation,
    FieldHasValueCheck,
    RunOutcome,
    StepRecord,
    TextVisibleCheck,
    Trace,
    UIElement,
    UIState,
    UrlContainsCheck,
)

# Whether a step belongs in the capability is decided by whether its *action*
# ran, not by whether its expectation held.
#
# Those are different questions, and conflating them silently drops steps the
# flow depends on. A click that navigated correctly but whose planner-declared
# expectation was a bad guess shows up in the trace as `verify_failed` -- and
# discarding it produced a capability that typed credentials and then tried to
# search without ever having submitted the login form.

# Actions that carry no value and target nothing.
TARGETLESS_ACTIONS = {ActionKind.NAVIGATE, ActionKind.WAIT, ActionKind.DONE}

# Stands in for a credential inside an expectation. Never compared: the
# verifier short-circuits any check naming a credential field.
CREDENTIAL_PLACEHOLDER = "<credential>"


class CompilerError(Exception):
    """The trace cannot become a capability."""


def compile_capability(
    trace: Trace,
    capability_id: str | None = None,
    title: str | None = None,
    credentials: Mapping[str, str] | None = None,
) -> Capability:
    """Compile a completed discovery run into a `Capability`.

    `credentials` maps a field name (`username`, `password`) to the value the run
    used, so a typed value can be recognised as a secret by *what it was* and not
    only by the name of the box it went into. Without it, a username typed into a
    field called "Operator ID" looks exactly like an ordinary constant and would
    be written into the artifact.

    Raises `CompilerError` for a run that did not reach its goal: a capability
    built from a failed run would encode the failure.
    """
    if trace.outcome is not RunOutcome.COMPLETED:
        raise CompilerError(
            f"run {trace.run_id} ended {trace.outcome}, not completed; "
            "only a run that reached its goal can be compiled"
        )

    kept = _executed_steps(trace)
    if not kept:
        raise CompilerError(f"run {trace.run_id} has no successful steps to compile")

    done_step = next(
        (step for step in reversed(kept) if step.decision.intent.action is ActionKind.DONE),
        None,
    )
    if done_step is None:
        raise CompilerError(
            f"run {trace.run_id} never declared `done`, so it has no checkpoint to assert"
        )

    # `done` is the planner declaring completion; its expectation becomes the
    # checkpoint, and the step itself is not replayed.
    body = [step for step in kept if step is not done_step]

    params: dict[str, ParamSpec] = {}
    outputs: list[OutputSpec] = []
    steps: list[CapabilityStep] = []

    observed_after = _observed_states(kept)

    for new_index, step in enumerate(body):
        intent = step.decision.intent
        element = _element_for(step)
        value = _classify_value(step, element, trace.goal, params, credentials or {})

        if intent.action is ActionKind.READ:
            outputs.append(_output_for(step, new_index))

        steps.append(
            CapabilityStep(
                index=new_index,
                intent=intent.intent,
                action=intent.action,
                target=_target_for(
                    intent.action, element, intent.target_description, step.state_before.elements
                ),
                value=value,
                risk=intent.risk,
                expectation=_trustworthy_expectation(step, observed_after.get(step.index)),
            )
        )

    steps = [
        _sanitise_expectation(step, params, credentials or {}) for step in steps
    ]
    # `body` and `steps` are parallel by construction -- one `CapabilityStep`
    # per kept record, in order -- which is what lets a step be compared against
    # what its own action actually returned.
    steps = [
        _unquote_read_expectation(step, record) for step, record in zip(steps, body, strict=True)
    ]
    checkpoint = _resolve_checkpoint(done_step, body, steps)
    checkpoint = _bind_checkpoint_to_record(checkpoint, params, done_step, trace)
    steps, checkpoint = _sweep_credentials(steps, checkpoint, credentials or {})

    # Derived from the goal with the recorded values taken out. A capability
    # that keeps its own argument in its name -- `look_up_member_12345` -- reads
    # like a capability for one record, so a caller looking for the one that
    # handles member 22841 does not believe this is it. The parameter is what
    # varies; it cannot also be the identity.
    generic_goal = _without_param_literals(trace.goal, params)
    resolved_id = capability_id or _slug(generic_goal) or "capability"
    # The title is templated rather than stripped, because it is the line a
    # human and an agent read to decide whether this is the right capability.
    # "look up member {{member_id_or_name}} and read their savings balance"
    # says what it does *and* what it needs; the stripped version says neither.
    templated_title = title or _template_goal(trace.goal, params)
    return Capability(
        schema_version=SCHEMA_VERSION,
        id=resolved_id,
        title=templated_title,
        description=f"Recorded from discovery run {trace.run_id}.",
        surface=_surface_for(trace),
        inputs=list(params.values()),
        outputs=outputs,
        steps=steps,
        checkpoint=checkpoint,
        provenance=Provenance(
            discovery_run_id=trace.run_id,
            goal=trace.goal,
            model=done_step.decision.model,
            recorded_at=trace.started_at,
            source_step_count=len(trace.steps),
            retried_step_count=len(trace.steps) - len(kept),
        ),
    )


def recorded_urls(trace: Trace) -> tuple[dict[int, str], str]:
    """Where each compiled step landed, and where the run finished.

    A later pass that wants to assert something about a step's URL has to be
    able to check its claim against what actually happened, and the capability
    does not carry URLs -- deliberately, since they are the most tenant-specific
    thing in a recording. This exposes them from the trace instead, keyed by the
    *capability's* step numbering: the compiler renumbers after dropping failed
    attempts, so the trace's own indices do not line up with the artifact's.

    Same selection rules as `compile_capability`, so the two cannot disagree.
    """
    kept = _executed_steps(trace)
    done_step = next(
        (step for step in reversed(kept) if step.decision.intent.action is ActionKind.DONE),
        None,
    )
    body = [step for step in kept if step is not done_step]
    observed = _observed_states(kept)

    per_step = {}
    for new_index, step in enumerate(body):
        state = observed.get(step.index)
        if state is not None and state.url:
            per_step[new_index] = state.url
    final = (done_step.state_before.url or "") if done_step is not None else ""
    return per_step, final


# --------------------------------------------------------------------------- #
# Choosing which steps to keep
# --------------------------------------------------------------------------- #
def _executed_steps(trace: Trace) -> list[StepRecord]:
    """The steps that actually changed the world, without immediate repeats.

    Two rules. A step is only real if its action executed and reported success:
    anything the guard halted, or that never found its target, changed nothing
    and has no place in a capability. And when the loop retried an intent, the
    attempt and the retry are the same action performed twice, so only one is
    kept -- replaying both would type the same field twice or, worse, click the
    same button twice.
    """
    executed = [step for step in trace.steps if step.act is not None and step.act.ok]

    deduplicated: list[StepRecord] = []
    for step in executed:
        if deduplicated and _same_action(deduplicated[-1], step):
            continue
        deduplicated.append(step)
    return deduplicated


def _same_action(left: StepRecord, right: StepRecord) -> bool:
    """Whether two consecutive steps are the same action performed twice."""
    a, b = left.decision.intent, right.decision.intent
    return (a.action, a.target_description, a.value) == (
        b.action,
        b.target_description,
        b.value,
    )


def _observed_states(kept: list[StepRecord]) -> dict[int, UIState]:
    """The settled state each kept step actually produced.

    Taken from the *next kept step's* `state_before` rather than the next
    recorded step's. Those differ, and the difference matters: a retry that
    raced a navigation observes the old page and reports failure, so using it
    would attribute the previous page to a step that had already left it. The
    next step that actually ran is the first settled look at the result.
    """
    observed: dict[int, UIState] = {}
    for position, step in enumerate(kept[:-1]):
        observed[step.index] = kept[position + 1].state_before
    return observed


def _trustworthy_expectation(step: StepRecord, after: UIState | None) -> Expectation:
    """The step's expectation, or one rebuilt from what actually happened.

    When a step's expectation held during recording, it is a checked assertion
    and is kept. When it did not, it is a planner's guess that has already been
    proved wrong, and replaying it would fail every time for the same reason.

    In that case the observed post-state is the better source: it is ground
    truth rather than prediction. A URL change is the strongest signal available
    and is what the recording can assert honestly; without one there is nothing
    better to say, so the original is kept and will be judged on its merits.
    """
    recorded = step.decision.intent.expectation
    if step.verify is not None and step.verify.ok:
        return recorded
    if after is None:
        return recorded

    # A URL change is the strongest evidence available, so prefer it.
    if after.url != step.state_before.url:
        path = _url_path(after.url)
        if path:
            return Expectation(
                description=(
                    f"The page moved to {path!r}, which is what this step was "
                    "observed to do during recording."
                ),
                check=UrlContainsCheck(fragment=path),
            )

    # No navigation does not mean nothing happened. Submitting a form inside an
    # iframe leaves the top-level URL alone while changing the content
    # completely, which is exactly the case this branch exists for. Text that
    # appeared as a result of the step is the next best ground truth.
    appeared = _text_that_appeared(step.state_before, after)
    if appeared is not None:
        return Expectation(
            description=(
                f"{appeared!r} appeared on the page, which is what this step was "
                "observed to do during recording."
            ),
            check=TextVisibleCheck(text=appeared),
        )
    return recorded


# Long enough to be distinctive, short enough not to be a paragraph.
MIN_APPEARED_TEXT = 3
MAX_APPEARED_TEXT = 60


def _text_that_appeared(before: UIState, after: UIState) -> str | None:
    """A string the step brought onto the page, or None if nothing is usable.

    Taken in document order rather than by any cleverness about which string is
    most meaningful: the first new thing on the page is both deterministic and,
    in practice, the heading or count that announces the result.
    """
    seen = {_element_text(element) for element in before.elements}
    for element in after.elements:
        text = _element_text(element)
        if text and text not in seen and MIN_APPEARED_TEXT <= len(text) <= MAX_APPEARED_TEXT:
            return text
    return None


def _element_text(element: UIElement) -> str:
    """One element as a single comparable string."""
    name = element.name.strip()
    value = (element.value or "").strip()
    if not value or value == name:
        return name
    return f"{name} {value}".strip()


def _url_path(url: str) -> str:
    """The path portion of a URL, which is what a flow actually depends on."""
    match = re.match(r"^https?://[^/]+(/[^?#]*)", url)
    return match.group(1) if match else ""


# --------------------------------------------------------------------------- #
# Per-step decisions
# --------------------------------------------------------------------------- #
def _element_for(step: StepRecord) -> UIElement | None:
    """The element the step acted on, from the snapshot it was chosen in."""
    ref = step.decision.intent.target_ref
    if ref is None:
        return None
    return next((e for e in step.state_before.elements if e.ref == ref), None)


def _target_for(
    action: ActionKind,
    element: UIElement | None,
    described_as: str,
    snapshot: list[UIElement],
) -> TargetSpec | None:
    """Describe the target durably, or `None` for actions that have none.

    `match_index` is computed against the snapshot the step actually ran on, not
    assumed to be zero. A page with several "Open Record" links is the normal
    case, and replaying the wrong one would open the wrong member.
    """
    if action in TARGETLESS_ACTIONS or element is None:
        return None
    siblings = [
        candidate
        for candidate in snapshot
        if candidate.role == element.role
        and candidate.name == element.name
        and candidate.scope == element.scope
    ]
    return TargetSpec(
        role=element.role,
        name=element.name,
        scope=element.scope,
        match_index=siblings.index(element) if element in siblings else 0,
        described_as=described_as,
        anchor=_derive_anchor(element, snapshot),
    )


# A name that is really a value: currency, a bare number, a date. These are the
# shapes a table cell perceives as, because the accessibility tree has nothing
# else to call it. Matched on the *name*, so this is a statement about the
# target being unidentifiable, not about the data being sensitive.
_VALUE_SHAPED = re.compile(r"""
    ^\s*(?:
        [$£€]\s?[\d,]+(?:\.\d{2})?    # money
      | [\d,]+\.\d{2}                  # a bare decimal amount
      | \d{4}-\d{2}-\d{2}              # a date
      | \d{3,}                         # a bare identifier
    )\s*$
""", re.VERBOSE)


def _derive_anchor(element: UIElement, snapshot: list[UIElement]) -> AnchorSpec | None:
    """An anchor for a target whose name is its own value.

    This is a rule, not a judgement, which is why it lives in the compiler
    rather than in the semantic pass: an element whose accessible name is a
    currency amount has no name of its own, and that is decidable by looking at
    it. What the model cannot do here is more useful done deterministically --
    the anchor comes from the recorded snapshot, so it is a fact about the page
    rather than a guess about it.

    The anchor is the nearest preceding element with a name that is *not* itself
    value-shaped: on the target app that is the account type, `Savings`, which
    sits in the same table row. Returns None when there is no such neighbour,
    because an anchor nobody can resolve is worse than no anchor.
    """
    if not _VALUE_SHAPED.match(element.name or ""):
        return None
    try:
        position = snapshot.index(element)
    except ValueError:
        return None

    for distance in range(1, position + 1):
        candidate = snapshot[position - distance]
        name = (candidate.name or "").strip()
        if not name or _VALUE_SHAPED.match(name):
            continue
        return AnchorSpec(after_name=name, offset=distance, role=element.role)
    return None


def _classify_value(
    step: StepRecord,
    element: UIElement | None,
    goal: str,
    params: dict[str, ParamSpec],
    credentials: Mapping[str, str],
) -> StepValue | None:
    """Decide whether a typed value is a secret, an input, or a constant."""
    intent = step.decision.intent
    if intent.value is None:
        return None

    field_name = element.name if element is not None else intent.target_description

    # Rule 2a: recognised by the field it went into. This is checked *first*
    # because it is the stronger signal: a box called "Passcode" holds a
    # password whatever the value looks like. Deciding by value alone gets this
    # wrong whenever the two credentials happen to be the same string, which is
    # exactly what demo fixtures tend to do.
    if is_unobservable_field(field_name) or is_unobservable_field(intent.target_description):
        return CredentialRef(field="password")

    # Rule 2b: recognised by value. A username is a credential even though the
    # field it goes into ("Operator ID") reads like an ordinary one.
    for field in ("username", "password"):
        secret = credentials.get(field)
        if secret and intent.value == secret:
            return CredentialRef(field=field)

    # Rule 3: a value the goal already mentions is what the caller varies.
    if intent.value and intent.value in goal:
        name = _slug(field_name) or "input"
        params.setdefault(
            name,
            ParamSpec(
                name=name,
                description=f"Value typed into {field_name!r}.",
                example=intent.value,
            ),
        )
        return ParamRef(name=name)

    return Constant(value=intent.value)


def _output_for(step: StepRecord, step_index: int) -> OutputSpec:
    """Name the value a `read` produced, from what the planner said it was reading."""
    description = step.decision.intent.target_description or step.decision.intent.intent
    return OutputSpec(
        name=_slug(description) or f"value_{step_index}",
        description=description,
        source_step=step_index,
    )


def _surface_for(trace: Trace) -> SurfaceRef:
    """Split the recorded URL into an origin and an entry path.

    The origin is what changes between two institutions running the same vendor
    app; the path is what the capability actually depends on.
    """
    match = re.match(r"^(https?://[^/]+)(/.*)?$", trace.target_url)
    if match is None:
        return SurfaceRef(entry_path="/", recorded_origin=trace.target_url)
    return SurfaceRef(entry_path=match.group(2) or "/", recorded_origin=match.group(1))


# --------------------------------------------------------------------------- #
# Making expectations replayable
#
# The planner wrote its expectations about one concrete run: it quoted the
# member id it was given and the balance it happened to find. Left alone, those
# literals make a capability that only replays for the exact input it was
# recorded with -- and, worse, one that carries the credential it typed. This
# pass rewrites them so an expectation asserts the *shape* of success rather
# than one instance of it.
# --------------------------------------------------------------------------- #
def _sanitise_expectation(
    step: CapabilityStep, params: dict[str, ParamSpec], credentials: Mapping[str, str]
) -> CapabilityStep:
    """Strip secrets out of a step's expectation and template its parameters."""
    check = step.expectation.check

    # A page-text assertion that quotes a parameter's recorded value is the same
    # problem in a different check kind: "the text 12345 is visible" only holds
    # for the member that was recorded. Template it against whichever parameter
    # it matches, whether or not this step is the one that typed it.
    if isinstance(check, TextVisibleCheck):
        for param in params.values():
            if param.example and check.text == param.example:
                templated = check.model_copy(update={"text": _template(param.name)})
                return step.model_copy(
                    update={"expectation": step.expectation.model_copy(
                        update={"check": templated}
                    )}
                )
        return step

    if not isinstance(check, FieldHasValueCheck):
        return step

    # A credential step's expectation quoted the password. The verifier already
    # refuses to evaluate these, so the literal is dead weight *and* a leak.
    if isinstance(step.value, CredentialRef):
        new_check = check.model_copy(update={"expected": CREDENTIAL_PLACEHOLDER})
    elif isinstance(step.value, ParamRef) and step.value.name in params:
        # The literal is the recorded value of a parameter. Point at the
        # parameter instead so the check follows whatever the caller supplies.
        new_check = check.model_copy(update={"expected": _template(step.value.name)})
    else:
        return step

    expectation = step.expectation.model_copy(update={"check": new_check})
    return step.model_copy(update={"expectation": expectation})


def _unquote_read_expectation(step: CapabilityStep, record: StepRecord) -> CapabilityStep:
    """Stop a `read` step asserting the very value it exists to return.

    The planner writes "the total balance $515.50 is visible" as the
    expectation for the step that reads it. That assertion is both tautological
    -- it checks the number is there immediately before extracting it -- and
    pinned to one recording, so replay fails the day the figure changes. It is
    the same defect as a checkpoint quoting recorded data (`_resolve_checkpoint`
    above), one layer down, and it survived there because `generalize.apply`
    skips `READ` steps entirely.

    It does not need the semantic pass. "This expectation quotes what this
    step's own read returned" is two recorded strings compared, so the rule is
    deterministic and runs with no key, no network and no model -- which also
    means it cannot be undone by the semantic pass failing.

    Demoted, never dropped: replay asserts something at every step by design.
    The anchor is what the demotion reaches for, because the target is already
    located as "one past the element named Total" -- so `Total` being on the
    page is the structural fact this step was already relying on. Where there
    is no anchor and no name that is not the value itself, there is no
    structural claim to make and the expectation stands unchanged.
    """
    if step.action is not ActionKind.READ or record.act is None:
        return step
    read_value = record.act.read_value
    quoted = _quoted_text(step.expectation)
    if not read_value or quoted is None or quoted != read_value:
        return step

    anchor = step.target.anchor if step.target else None
    if anchor is not None:
        replacement = ElementVisibleCheck(
            kind="element_visible", role=anchor.role, name_contains=anchor.after_name
        )
        description = f"the {anchor.after_name} row is on the page"
    elif step.target and step.target.name and step.target.name != read_value:
        replacement = ElementVisibleCheck(
            kind="element_visible", role=step.target.role, name_contains=step.target.name
        )
        description = f"the {step.target.name} element is on the page"
    else:
        return step

    return step.model_copy(
        update={"expectation": Expectation(description=description, check=replacement)}
    )


def _resolve_checkpoint(
    done_step: StepRecord, body: list[StepRecord], steps: list[CapabilityStep]
) -> Expectation:
    """Choose a checkpoint that can be true for inputs other than the recorded one.

    The planner's `done` expectation usually quotes the answer it just found --
    "the savings balance $4,812.55 is visible". That is a perfect assertion for
    exactly one member and a guaranteed failure for every other, so it cannot be
    the checkpoint of a parameterised capability.

    When the `done` expectation quotes a value that a `read` step produced, the
    last real step's expectation is used instead: it is what the recording
    already asserted about *reaching the right page*, and it does not name the
    answer. Otherwise the planner's own checkpoint stands.
    """
    checkpoint = done_step.decision.intent.expectation
    quoted = _quoted_text(checkpoint)
    if quoted is None:
        return checkpoint

    read_values = {
        step.act.read_value
        for step in body
        if step.decision.intent.action is ActionKind.READ and step.act and step.act.read_value
    }
    if quoted not in read_values or not steps:
        return checkpoint

    # Walk back to the last step that asserted something other than the answer.
    # The `read` step's own expectation usually quotes the same literal, so
    # taking the last step blindly would reintroduce the problem it is here to
    # solve. If every step quoted the answer there is nothing better to use, and
    # the planner's checkpoint stands rather than being silently weakened.
    for step in reversed(steps):
        literal = _quoted_text(step.expectation)
        if literal is None or literal not in read_values:
            return step.expectation
    return checkpoint


def _binds_a_parameter(expectation: Expectation) -> bool:
    """Whether this check's literal follows the caller's input rather than the
    recorded one. A `{{name}}` placeholder is the only way it can."""
    check = expectation.check
    for attribute in ("text", "expected", "fragment", "name_contains"):
        literal = getattr(check, attribute, None)
        if isinstance(literal, str) and "{{" in literal:
            return True
    return False


def _bind_checkpoint_to_record(
    checkpoint: Expectation,
    params: Mapping[str, ParamSpec],
    done_step: StepRecord,
    trace: Trace,
) -> Expectation:
    """Make a parameterised capability's checkpoint say *which* record it reached.

    `_resolve_checkpoint` above stops the checkpoint from quoting the recorded
    answer. That is necessary and not sufficient: what it usually leaves behind
    is a claim like "the text 'Savings' is visible", which is true on every
    member's page. Ask for member 22841, land on member 12345, read the wrong
    balance -- and the checkpoint says success. A success check that cannot tell
    those two runs apart is not checking the thing the caller cares about.

    So this is a rule rather than a judgement, for the same reason the rest of
    the compiler is: if the capability takes an input and the checkpoint names
    no parameter, the checkpoint is replaced with one asserting that the URL
    identifies the requested record.

    The recorded final URL has to be consulted, not assumed. Picking the first
    parameter that has an example and templating it in produces a checkpoint
    that fails on every invocation when that value never appeared in the URL --
    a check that is wrong 100% of the time instead of wrong when it matters. If
    no parameter can be shown to identify the record, this refuses to compile
    rather than emitting a checkpoint that cannot discriminate: the same call
    `store.load` makes for an unreadable schema_version.
    """
    if not params or _binds_a_parameter(checkpoint):
        return checkpoint

    final_url = done_step.state_before.url or ""
    ordered = sorted(params.values(), key=lambda spec: len(spec.example or ""), reverse=True)
    for spec in ordered:
        if spec.example and spec.example in final_url:
            fragment = _template(spec.name)
            return Expectation(
                description=f"The URL identifies the requested record ({fragment}).",
                check=UrlContainsCheck(kind="url_contains", fragment=fragment),
            )

    examples = [spec.example for spec in ordered]
    raise CompilerError(
        f"run {trace.run_id} takes {sorted(params)} but has no checkpoint that can "
        f"distinguish one record from another: the recorded checkpoint binds no "
        f"parameter, and the final url {final_url!r} contains none of the recorded "
        f"values {examples}. Refusing to emit a capability whose success check would "
        f"pass for the wrong record."
    )


def _quoted_text(expectation: Expectation) -> str | None:
    """The literal a check asserts, when it asserts one.

    `element_visible` belongs here for the same reason the other two do: a
    planner that ends a run with `name_contains: "$515.50"` has quoted the
    answer just as surely as one that writes `text_visible: "$515.50"`, and
    leaving it out let a recorded balance survive into a checkpoint.

    `url_contains.fragment` is left out on purpose. A URL that carries a value
    the run read is usually how a lookup names its record -- that checkpoint
    wants binding to a parameter, which `_bind_checkpoint_to_record` does, not
    discarding as overfitted.
    """
    check = expectation.check
    if isinstance(check, TextVisibleCheck):
        return check.text
    if isinstance(check, FieldHasValueCheck):
        return check.expected
    if isinstance(check, ElementVisibleCheck):
        return check.name_contains
    return None


def _template(param_name: str) -> str:
    """How a parameter is referenced inside an expectation literal."""
    return f"{{{{{param_name}}}}}"


def _with_param_literals_replaced(text: str, params: Mapping[str, ParamSpec], replacement) -> str:
    """`text` with every recorded parameter value swapped for `replacement(spec)`.

    Longest example first: where one example is a substring of another,
    replacing the short one would corrupt the long one before it is reached.
    """
    ordered = sorted(params.values(), key=lambda spec: len(spec.example or ""), reverse=True)
    for spec in ordered:
        if spec.example:
            text = text.replace(spec.example, replacement(spec))
    return text


def _without_param_literals(goal: str, params: Mapping[str, ParamSpec]) -> str:
    """The goal with the recorded argument values removed -- the part that does
    not change from one invocation to the next, which is what an id names."""
    return _with_param_literals_replaced(goal, params, lambda _: " ")


def _template_goal(goal: str, params: Mapping[str, ParamSpec]) -> str:
    """The goal with each recorded argument value replaced by its placeholder."""
    return _with_param_literals_replaced(goal, params, lambda spec: _template(spec.name))


def _sweep_credentials(
    steps: list[CapabilityStep], checkpoint: Expectation, credentials: Mapping[str, str]
) -> tuple[list[CapabilityStep], Expectation]:
    """Remove credential values from the free text the planner wrote.

    Structured fields are handled where they are built, but the planner also
    narrates: "Type the username 'operator' into...". That prose is kept because
    it is what makes an artifact reviewable, so it has to be swept rather than
    dropped. Same lesson as the evidence writer -- a secret ends up wherever
    text is generated, so the sweep has to cover all of it, not one field.
    """
    secrets = sorted({value for value in credentials.values() if value}, key=len, reverse=True)
    if not secrets:
        return steps, checkpoint

    def scrub(value: object) -> object:
        if isinstance(value, str):
            for secret in secrets:
                value = value.replace(secret, CREDENTIAL_PLACEHOLDER)
            return value
        if isinstance(value, dict):
            return {key: scrub(item) for key, item in value.items()}
        if isinstance(value, list):
            return [scrub(item) for item in value]
        return value

    swept_steps = [CapabilityStep.model_validate(scrub(s.model_dump())) for s in steps]
    swept_checkpoint = Expectation.model_validate(scrub(checkpoint.model_dump()))
    return swept_steps, swept_checkpoint


# --------------------------------------------------------------------------- #
# Naming
# --------------------------------------------------------------------------- #
# Words that add nothing to a derived name. Trimmed so that "the savings balance
# for member 12345" becomes `savings_balance_member`, not a sentence.
NAME_STOPWORDS = frozenset(
    {"a", "an", "and", "for", "from", "in", "into", "of", "on", "the", "to", "value", "field"}
)

MAX_NAME_WORDS = 4


def _slug(text: str) -> str:
    """A stable snake_case identifier derived from human text.

    Derived, not invented: compiling the same trace twice must produce the same
    contract, so this cannot involve a model or a counter.
    """
    words = [word for word in re.split(r"[^a-zA-Z0-9]+", text.casefold()) if word]
    meaningful = [word for word in words if word not in NAME_STOPWORDS] or words
    return "_".join(meaningful[:MAX_NAME_WORDS])
