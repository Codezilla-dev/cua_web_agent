"""Execute a `Capability` against a live surface, with no model in the loop.

This is the production path: the discovery loop is how a flow is learned once,
this is how it is run a thousand times. The difference is not efficiency, it is
determinism — the same artifact and the same inputs must do the same thing, so
nothing here consults a planner. `src.agent.planner` is not imported, and a test
asserts that it never becomes imported.

The loop per step is deliberately the same shape as discovery's, minus the
deciding: resolve the target, guard the action, perform it, let the page settle,
then verify the step's expectation. Reusing `verify_expectation` matters — a
capability's expectations were written and checked by that function during
recording, so replay judging them by different rules would be a silent source of
disagreement between the two paths.

Three things replay does that discovery does not:

**Bind parameters.** Recorded literals were templated into `{{name}}` by the
compiler; they are substituted back here from the caller's arguments, in both
step values and expectation text.

**Watch for a definitive application answer.** After every step the page is
checked against the configured business-outcome rules. "Record Not Found" ends
the replay with an answer rather than an error, because that is what it is.

**Assert the checkpoint.** Walking every step is not success. The capability
names a condition that proves the flow arrived where it should, and it is
checked before any output is returned.
"""

import time
from typing import Any, NamedTuple

from src.agent.verify import is_unobservable_field, verify_expectation
from src.artifact.compiler import CREDENTIAL_PLACEHOLDER
from src.artifact.models import (
    Capability,
    CapabilityStep,
    Constant,
    CredentialRef,
    ParamRef,
    TargetSpec,
)
from src.escalation.conditions import StuckCondition
from src.escalation.intervention import STEP_ALREADY_PERFORMED
from src.policy.check import Policy
from src.replay.escalate import EscalationPolicy
from src.replay.evidence import ReplayEvidenceWriter, ReplayStepRecord
from src.replay.outcomes import (
    BusinessOutcome,
    BusinessOutcomeRule,
    FailureKind,
    HardFailure,
    Recovery,
    ReplayResult,
    Success,
    detect_business_outcome,
)
from src.surface.base import Surface, SurfaceError
from src.types import (
    ActionKind,
    Expectation,
    Intent,
    ResolvedTarget,
    RiskTier,
    UIElement,
    UIState,
)

# A step whose target is missing is retried once after a further settle: the
# usual cause is a page that had not finished rendering, not a changed app.
TARGET_RETRY_ATTEMPTS = 1

# Actions whose effect is supposed to show up in the page's text. A `type` that
# leaves the digest alone is normal -- the value goes into an input, not into
# innerText -- so counting it as "no progress" would be wrong.
DIGEST_VISIBLE_ACTIONS = {ActionKind.NAVIGATE, ActionKind.CLICK}

# How many acting steps in a row may change nothing before a failure is reported
# as "the page is not moving" rather than as whatever it locally looked like.
# Two, not one: a single click that renders an identical page happens (a form
# that re-serves itself), and the point of the condition is a *pattern*.
NO_PROGRESS_THRESHOLD = 2


class ReplayRequest:
    """Everything one replay needs. A small class, not a bag of arguments."""

    def __init__(
        self,
        capability: Capability,
        params: dict[str, str],
        surface: Surface,
        policy: Policy,
        credentials: dict[str, str],
        business_rules: list[BusinessOutcomeRule],
        settle_timeout_s: float,
        evidence_dir: str | None = None,
        evidence: ReplayEvidenceWriter | None = None,
        escalation: EscalationPolicy | None = None,
    ) -> None:
        self.capability = capability
        self.params = params
        self.surface = surface
        self.policy = policy
        self.credentials = credentials
        self.business_rules = business_rules
        self.settle_timeout_s = settle_timeout_s
        self.evidence = evidence
        self.escalation = escalation
        # A result carries a *reference* to its evidence; the writer is what
        # produces it. Deriving one from the other keeps them from disagreeing.
        self.evidence_dir = (
            evidence_dir if evidence is None else str(evidence.evidence_dir)
        )


class _RunProgress:
    """What is only visible by looking across steps rather than at one.

    Two conditions the brief names cannot be seen from inside a single step,
    because at each individual step nothing has gone wrong:

    **No progress.** A click that no longer does anything, followed by an
    expectation loose enough to hold on the unchanged page ("the URL still
    contains /search"), passes. The run walks on, going nowhere. The comparison
    is the page's text digest -- the same signal the discovery loop uses for its
    no-op check, so the two agree about what "the page changed" means.

    **Auth expired.** A password field on screen is unremarkable during login
    and means exactly one thing after it, so what has to be remembered is that
    the flow already signed in. Two events, not one: a step carrying a
    `CredentialRef` arms the detector, and the password field then *going away*
    is what marks the sign-in as having happened. Reading the credential step
    alone as the sign-in armed the detector during login itself, where the
    password box it was watching for was the one the flow had just filled in.
    """

    def __init__(self) -> None:
        self.streak = 0
        self.credentials_entered = False
        self.signed_in = False

    def note_credential_used(self) -> None:
        """A step carrying a `CredentialRef` ran. That arms the detector only.

        Typing a password is not signing in; submitting it is. Treating the
        credential step as the sign-in left the detector armed *during* login,
        looking at the very field the step had just filled -- so a ParaBank
        replay that could not find its renamed "Log In" button escalated as
        `auth_expired` and sent the operator to re-authenticate a session that
        had never authenticated.
        """
        self.credentials_entered = True

    def record(self, action: ActionKind, before: UIState, after: UIState) -> None:
        self._note_sign_in(after)
        if action not in DIGEST_VISIBLE_ACTIONS:
            return
        if before.text_digest == after.text_digest:
            self.streak += 1
        else:
            self.streak = 0

    def _note_sign_in(self, state: UIState) -> None:
        """Signed in is *observed*: the password field has to have gone away.

        That is what the event consists of, and it needs nothing from the
        capability to detect -- which is why it is checked on every step's
        post-action state rather than inferred from the step's declared value.
        """
        if self.credentials_entered and not self._has_password_field(state):
            self.signed_in = True

    @property
    def stalled(self) -> bool:
        return self.streak >= NO_PROGRESS_THRESHOLD

    def signed_out(self, state: UIState) -> bool:
        """A password box is back, after the flow had demonstrably signed in."""
        if not self.signed_in:
            return False
        return self._has_password_field(state)

    @staticmethod
    def _has_password_field(state: UIState) -> bool:
        return any(
            element.role == "textbox" and is_unobservable_field(element.name)
            for element in state.elements
        )

    def hint(self, state: UIState) -> str | None:
        """The `StuckCondition` this run's history says we are really in, if any.

        Auth first: if the session is dead, "the page stopped changing" is a
        true observation about a false cause, and sending an operator to look
        for a stuck widget when they need to sign in wastes the handoff.
        """
        if self.signed_out(state):
            return StuckCondition.AUTH_EXPIRED.value
        if self.stalled:
            return StuckCondition.NO_PROGRESS_N_STEPS.value
        return None


def replay(request: ReplayRequest) -> ReplayResult:
    """Run `request.capability` and report one of three outcomes.

    Thin on purpose: `_replay` has several exits, and the result must be
    recorded on all of them. Wrapping is how that stays true when a new exit
    is added later.
    """
    result = _replay(request)
    if request.evidence is not None:
        request.evidence.write_result(result, request.capability.id, request.params)
    return result


def _replay(request: ReplayRequest) -> ReplayResult:
    capability = request.capability
    missing = [
        spec.name
        for spec in capability.inputs
        if spec.required and not request.params.get(spec.name)
    ]
    if missing:
        return HardFailure(
            kind=FailureKind.INVALID_REQUEST,
            step_index=-1,
            expected=f"values for required inputs {missing}",
            observed=f"got {sorted(request.params)}",
            evidence_dir=request.evidence_dir,
        )

    recoveries: list[Recovery] = []
    outputs: dict[str, str] = {}
    outputs_by_step = {spec.source_step: spec.name for spec in capability.outputs}
    # Mutable, threaded through the step calls rather than made a field of
    # `ReplayRequest`: the request describes the invocation and is read-only,
    # while this is state that only exists while the run is happening.
    progress = _RunProgress()

    try:
        # `start` is inside the try, not before it. A browser that will not
        # launch is the most likely surface error there is, and leaving it
        # outside meant the one failure SURFACE_ERROR exists to describe was the
        # one that escaped as an exception instead. `stop` tolerates a surface
        # that never started, so the `finally` below is still safe.
        request.surface.start()
        request.surface.goto(capability.surface.recorded_origin + capability.surface.entry_path)
        request.surface.settle.wait_until_settled(request.settle_timeout_s)

        for step in capability.steps:
            result = _run_step(request, step, recoveries, outputs, outputs_by_step, progress)
            if result is not None:
                result = _escalate_and_retry(
                    request, step, result, recoveries, outputs, outputs_by_step, progress
                )
            if result is not None:
                return result

        checkpoint = _assert_checkpoint(request, outputs, recoveries)
        return _escalate_checkpoint(request, checkpoint, outputs, recoveries)
    except SurfaceError as error:
        return HardFailure(
            kind=FailureKind.SURFACE_ERROR,
            step_index=-1,
            expected="a working surface",
            observed=str(error),
            evidence_dir=request.evidence_dir,
        )
    finally:
        request.surface.stop()


# --------------------------------------------------------------------------- #
# Escalation
# --------------------------------------------------------------------------- #
def _escalate_and_retry(
    request: ReplayRequest,
    step: CapabilityStep,
    result: ReplayResult,
    recoveries: list[Recovery],
    outputs: dict[str, str],
    outputs_by_step: dict[int, str],
    progress: "_RunProgress",
) -> ReplayResult | None:
    """Hand a stuck step to a person, then try it once more.

    Returns `None` when the retry succeeded, which puts the run back on the
    normal path -- the caller cannot tell the difference between a step that
    worked first time and one a human unblocked, which is the point.
    """
    policy = request.escalation
    if not isinstance(result, HardFailure) or policy is None:
        return result
    if not policy.should_escalate(result):
        return result

    intervention = policy.escalate(result, request)
    if intervention is None:
        # Nobody came. The original failure is the honest answer.
        return result

    disposition = intervention.read_disposition()
    recoveries.append(
        Recovery(
            step_index=step.index,
            condition=f"escalated: {result.kind.value}",
            action_taken=(
                f"human took the session ({intervention.intervention_id}) "
                f"and said the step was {disposition}"
            ),
            attempts=1,
        )
    )
    if disposition == STEP_ALREADY_PERFORMED:
        # The operator did this step by hand. Doing it again would do it twice,
        # and for a submit or a transfer that is not a cosmetic problem.
        return None
    return _run_step(request, step, recoveries, outputs, outputs_by_step, progress)


def _escalate_checkpoint(
    request: ReplayRequest,
    result: ReplayResult,
    outputs: dict[str, str],
    recoveries: list[Recovery],
) -> ReplayResult:
    """A flow that walked every step but landed wrong is worth a person's look."""
    policy = request.escalation
    if not isinstance(result, HardFailure) or policy is None:
        return result
    if not policy.should_escalate(result):
        return result

    intervention = policy.escalate(result, request)
    if intervention is None:
        return result

    recoveries.append(
        Recovery(
            step_index=result.step_index,
            condition=f"escalated: {result.kind.value}",
            action_taken=f"human took the session ({intervention.intervention_id})",
            attempts=1,
        )
    )
    return _assert_checkpoint(request, outputs, recoveries)


# --------------------------------------------------------------------------- #
# One step
# --------------------------------------------------------------------------- #
def _run_step(
    request: ReplayRequest,
    step: CapabilityStep,
    recoveries: list[Recovery],
    outputs: dict[str, str],
    outputs_by_step: dict[int, str],
    progress: "_RunProgress",
) -> ReplayResult | None:
    """Run one step, recording what happened whichever way it ends.

    The record is written in a `finally` rather than at each exit because
    `_execute_step` returns from seven places, and evidence that is missing
    exactly when a step failed is evidence for the case it is least useful in.
    """
    started = time.monotonic()
    record = ReplayStepRecord(
        index=step.index,
        intent=step.intent,
        action=step.action,
        target_role=step.target.role if step.target else None,
        target_name=step.target.name if step.target else None,
        target_scope=step.target.scope if step.target else None,
        target_match_index=step.target.match_index if step.target else None,
        value_kind=step.value.kind if step.value else None,
    )
    try:
        return _execute_step(
            request, step, recoveries, outputs, outputs_by_step, record, progress
        )
    finally:
        record.duration_ms = int((time.monotonic() - started) * 1000)
        if request.evidence is not None:
            request.evidence.append_step(record)


def _execute_step(
    request: ReplayRequest,
    step: CapabilityStep,
    recoveries: list[Recovery],
    outputs: dict[str, str],
    outputs_by_step: dict[int, str],
    record: ReplayStepRecord,
    progress: "_RunProgress",
) -> ReplayResult | None:
    """Run one step. Returns a result only when the replay should stop."""
    state = request.surface.perception.perceive()

    target: ResolvedTarget | None = None
    if step.target is not None:
        target, state, recovered, strategy = _resolve_target(request, step, state)
        if recovered is not None:
            recoveries.append(recovered)
            record.recovery_note = recovered.action_taken
        record.resolved = target is not None
        record.strategy_used = strategy
        if target is None:
            # A control that has vanished because the session was signed out
            # is not a locator problem, and sending the operator to hunt for a
            # renamed control when they need to log back in wastes the handoff.
            return HardFailure(
                kind=FailureKind.TARGET_NOT_FOUND,
                step_index=step.index,
                step_intent=step.intent,
                expected=(
                    f"{step.target.role} named {step.target.name!r} "
                    f"in {step.target.scope} (match #{step.target.match_index})"
                ),
                observed=_describe_page(state),
                evidence_dir=request.evidence_dir,
                stuck_hint=progress.hint(state),
            )

    if isinstance(step.value, CredentialRef):
        progress.note_credential_used()

    intent = _intent_for(request, step, target)

    verdict = request.policy.check(intent, state, request.capability.surface.recorded_origin)
    record.policy_disposition = verdict.disposition
    record.policy_rule = verdict.rule
    if verdict.disposition == "halt":
        return HardFailure(
            kind=FailureKind.POLICY_HALTED,
            step_index=step.index,
            step_intent=step.intent,
            expected="an action the policy permits",
            observed=f"{verdict.rule}: {verdict.reason}",
            evidence_dir=request.evidence_dir,
        )

    act = request.surface.actions.execute(intent, target)
    record.act_ok = act.ok
    record.act_error = act.error
    record.read_value = act.read_value
    if not act.ok:
        return HardFailure(
            kind=FailureKind.ACTION_FAILED,
            step_index=step.index,
            step_intent=step.intent,
            expected=f"{step.action} to succeed",
            observed=act.error or "the action reported failure without a reason",
            evidence_dir=request.evidence_dir,
        )

    if act.read_value is not None and step.index in outputs_by_step:
        outputs[outputs_by_step[step.index]] = act.read_value

    request.surface.settle.wait_until_settled(request.settle_timeout_s)
    after = request.surface.perception.perceive()

    progress.record(step.action, state, after)
    record.no_progress_steps = progress.streak

    # A definitive answer from the application outranks the step's expectation:
    # if the record does not exist, the next step was never going to work, and
    # reporting "expectation failed" would describe the symptom, not the cause.
    matched = detect_business_outcome(request.business_rules, _page_text(after))
    if matched is not None:
        record.business_outcome = matched.code
        return BusinessOutcome(
            code=matched.code,
            detail=_matching_line(after, matched),
            step_index=step.index,
            recoveries=recoveries,
        )

    return _verify_step(request, step, after, outputs, outputs_by_step, record, progress)


def _verify_step(
    request: ReplayRequest,
    step: CapabilityStep,
    after: UIState,
    outputs: dict[str, str],
    outputs_by_step: dict[int, str],
    record: ReplayStepRecord,
    progress: "_RunProgress",
) -> ReplayResult | None:
    """Check the step's expectation, with one exception for reads.

    A `read` step's recorded expectation quotes the value it read -- "the balance
    $4,812.55 is visible". That is circular: it can only hold for the input it
    was recorded with, and asserting it would make every other input look like a
    failure. What actually matters for a read is that it returned something, so
    that is what is asserted instead.
    """
    if step.action is ActionKind.READ:
        name = outputs_by_step.get(step.index)
        value = outputs.get(name or "", "")
        record.check_performed = f"output {name!r} is non-empty"
        record.verify_ok = bool(value.strip())
        if value.strip():
            return None
        return HardFailure(
            kind=FailureKind.EXPECTATION_FAILED,
            step_index=step.index,
            step_intent=step.intent,
            expected=f"a non-empty value for output {name!r}",
            observed="the read returned nothing",
            evidence_dir=request.evidence_dir,
        )

    bound = _bind_expectation(
        step.expectation, request.params, _credential_for(request, step)
    )
    verified = verify_expectation(bound, after)
    record.verify_ok = verified.ok
    record.check_performed = verified.check_performed
    record.observed = verified.evidence
    if verified.ok:
        return None
    return HardFailure(
        kind=FailureKind.EXPECTATION_FAILED,
        step_index=step.index,
        step_intent=step.intent,
        expected=verified.check_performed,
        observed=verified.evidence,
        evidence_dir=request.evidence_dir,
        stuck_hint=progress.hint(after),
    )


def _assert_checkpoint(
    request: ReplayRequest, outputs: dict[str, str], recoveries: list[Recovery]
) -> ReplayResult:
    """Walking every step is not success; arriving where the capability says is."""
    state = request.surface.perception.perceive()
    bound = _bind_expectation(request.capability.checkpoint, request.params)
    verified = verify_expectation(bound, state)
    if not verified.ok:
        return HardFailure(
            kind=FailureKind.CHECKPOINT_FAILED,
            step_index=len(request.capability.steps),
            step_intent="checkpoint",
            expected=verified.check_performed,
            observed=verified.evidence,
            evidence_dir=request.evidence_dir,
        )
    return Success(
        outputs=outputs, steps_run=len(request.capability.steps), recoveries=recoveries
    )


# --------------------------------------------------------------------------- #
# Targets, values, expectations
# --------------------------------------------------------------------------- #
def _resolve_target(
    request: ReplayRequest, step: CapabilityStep, state: UIState
) -> tuple[ResolvedTarget | None, UIState, Recovery | None, str | None]:
    """Find the described control, retrying once after a further settle.

    A missing target is usually a page that had not finished rendering rather
    than an app that changed shape, so one more settle is worth trying before
    reporting a failure a human would have to look at.

    Returns the strategy that resolved it alongside the target, so the step
    record can say which rung of the chain the page is currently on.
    """
    assert step.target is not None
    for attempt in range(TARGET_RETRY_ATTEMPTS + 1):
        match = _match_element(state, step)
        if match is not None:
            resolved = request.surface.perception.resolve(match.element.ref, state)
            recovery = (
                Recovery(
                    step_index=step.index,
                    condition="target not present on first look",
                    action_taken="settled again and re-perceived",
                    attempts=attempt,
                )
                if attempt
                else None
            )
            return resolved, state, recovery, match.strategy
        if attempt < TARGET_RETRY_ATTEMPTS:
            request.surface.settle.wait_until_settled(request.settle_timeout_s)
            state = request.surface.perception.perceive()
    return None, state, None, None


class Match(NamedTuple):
    """A resolved element and the strategy that found it.

    The strategy travels with the element because it is the interesting half.
    "The step resolved" says nothing; "the step resolved, but only after
    collapsing case and whitespace" says the page has drifted and this
    capability is one rename away from breaking.
    """

    element: UIElement
    strategy: str


def _normalise(name: str) -> str:
    """Case-folded, whitespace-collapsed. The two differences that are almost
    never meaningful and are the most common way a rendered name shifts."""
    return " ".join(name.casefold().split())


def _candidates(state: UIState, spec: TargetSpec) -> list[UIElement]:
    """Elements of the right role in the right frame. Role and scope are never
    relaxed: a link is not a button, and the main document is not an iframe."""
    return [
        element
        for element in state.elements
        if element.role == spec.role and element.scope == spec.scope
    ]


def _nth(matches: list[UIElement], spec: TargetSpec, strategy: str) -> Match | None:
    """The occurrence the recording used, if this strategy found that many.

    `match_index` is honoured rather than always taking the first: a page with
    several identically-named links is normal, and the recording knows which one
    it used. If there are now fewer matches than the recording saw, that is a
    real change and resolving fails rather than guessing.
    """
    if len(matches) <= spec.match_index:
        return None
    return Match(matches[spec.match_index], strategy)


def _match_element(state: UIState, step: CapabilityStep) -> Match | None:
    """The element the step's `TargetSpec` describes, and how it was found.

    A ranked chain, strongest claim first, stopping at the first strategy that
    resolves. Each rung gives up something, so the order is the order of how
    much is being given up -- and the rung that fired is recorded, because a
    capability that has quietly slid down to `contains` is drifting even while
    it still works.

        anchor      position next to a stable neighbour
        exact       role + name + scope, character for character
        normalised  the same, with case and whitespace collapsed
        contains    the recorded name appears within the rendered one
        (fail)

    Role and scope are never relaxed. A link is not a button and the main
    document is not an iframe; loosening those would not be tolerating drift, it
    would be resolving a different control.
    """
    spec = step.target
    assert spec is not None

    # Anchor first, and not as a fallback, because when an anchor is present the
    # name is the thing *known* to be wrong: the compiler only sets one when the
    # name is the recorded value -- a balance, an id -- which by construction
    # will not match a different record. Trying names first would find the right
    # element only for the input it was recorded with, and `contains` on a
    # number like "$4,812.55" could match something else entirely.
    if spec.anchor is not None:
        anchored = _match_by_anchor(state, spec)
        if anchored is not None:
            return Match(anchored, "anchor")

    candidates = _candidates(state, spec)

    exact = _nth([e for e in candidates if e.name == spec.name], spec, "exact")
    if exact is not None:
        return exact

    wanted = _normalise(spec.name)
    if not wanted:
        return None

    normalised = _nth(
        [e for e in candidates if _normalise(e.name) == wanted], spec, "normalised"
    )
    if normalised is not None:
        return normalised

    return _nth(
        [e for e in candidates if wanted in _normalise(e.name)], spec, "contains"
    )


def _match_by_anchor(state: UIState, spec: TargetSpec) -> UIElement | None:
    """The element sitting `offset` past the named anchor, in the same scope.

    Perception publishes a flat, ordered list, so "just after the cell that says
    Savings" is expressible and stable where the balance's own name is not. The
    role is re-checked at the landing position: if the page grew a row and the
    offset now points at something else, failing is right -- a wrong value read
    confidently is worse than a resolution error.
    """
    anchor = spec.anchor
    if anchor is None:
        return None
    scoped = [element for element in state.elements if element.scope == spec.scope]
    for position, element in enumerate(scoped):
        if (element.name or "").strip() != anchor.after_name:
            continue
        landing = position + anchor.offset
        if landing >= len(scoped):
            continue
        candidate = scoped[landing]
        if anchor.role is not None and candidate.role != anchor.role:
            continue
        return candidate
    return None


def _intent_for(
    request: ReplayRequest, step: CapabilityStep, target: ResolvedTarget | None
) -> Intent:
    """Rebuild the `Intent` the executor and the policy both expect.

    Replay reuses discovery's action executor and guard rather than reimplementing
    them, so it has to speak the same vocabulary. `reasoning` says plainly that
    no model was involved, because this object ends up in logs beside ones that
    a planner did produce.
    """
    return Intent(
        reasoning="replayed from a recorded capability; no model was consulted",
        intent=step.intent,
        action=step.action,
        target_ref=target.ref if target is not None else None,
        target_description=step.target.described_as if step.target else "",
        value=_bind_value(request, step),
        risk=step.risk or RiskTier.SAFE,
        expectation=_bind_expectation(
            step.expectation, request.params, _credential_for(request, step)
        ),
    )


def _bind_value(request: ReplayRequest, step: CapabilityStep) -> str | None:
    """Resolve a step's recorded value slot into the string to type."""
    value = step.value
    if value is None:
        return None
    if isinstance(value, Constant):
        return value.value
    if isinstance(value, ParamRef):
        return request.params.get(value.name, "")
    if isinstance(value, CredentialRef):
        # The artifact never held this; it comes from configuration at run time.
        return request.credentials.get(value.field, "")
    return None


def _bind_expectation(
    expectation: Expectation, params: dict[str, str], credential: str | None = None
) -> Expectation:
    """Substitute the placeholders the compiler left in an expectation's literals.

    Expectations bind exactly like step values do, and for the same reason: the
    compiler removed the recorded literals so the artifact would carry neither a
    secret nor one caller's input, which means both have to be put back here.
    Binding only the value and not the expectation is how a step ends up typing
    the right thing and then asserting the wrong one.
    """
    bindings = dict(params)
    if credential is not None:
        bindings[CREDENTIAL_PLACEHOLDER] = credential

    if not bindings:
        return expectation
    payload: dict[str, Any] = expectation.model_dump()
    payload = _substitute_literals(payload, params, credential)
    return Expectation.model_validate(payload)


def _substitute_literals(
    value: Any, params: dict[str, str], credential: str | None
) -> Any:
    if isinstance(value, str):
        for name, supplied in params.items():
            value = value.replace(f"{{{{{name}}}}}", supplied)
        if credential is not None:
            value = value.replace(CREDENTIAL_PLACEHOLDER, credential)
        return value
    if isinstance(value, dict):
        return {
            key: _substitute_literals(item, params, credential)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_substitute_literals(item, params, credential) for item in value]
    return value


def _credential_for(request: ReplayRequest, step: CapabilityStep) -> str | None:
    """The secret this step uses, when it uses one."""
    if isinstance(step.value, CredentialRef):
        return request.credentials.get(step.value.field, "")
    return None


# --------------------------------------------------------------------------- #
# Describing what was there
# --------------------------------------------------------------------------- #
def _element_text(element: UIElement) -> str:
    """One element as a single line.

    A readable text node carries the same string in both `name` and `value`,
    so joining them blindly reports every message twice.
    """
    name = element.name.strip()
    value = (element.value or "").strip()
    if not value or value == name:
        return name
    return f"{name} {value}".strip()


def _page_text(state: UIState) -> str:
    """Everything perception saw, as one string, for rule matching."""
    return "\n".join(_element_text(element) for element in state.elements)


def _matching_line(state: UIState, rule: BusinessOutcomeRule) -> str:
    """The specific line that triggered a business outcome, for the caller's log."""
    for element in state.elements:
        line = _element_text(element)
        if line and rule.matches(line):
            return line
    return rule.code


def _describe_page(state: UIState) -> str:
    """A short account of what was on screen, for a target-not-found failure."""
    sample = [f"{e.role}:{e.name!r}" for e in state.elements if e.name][:10]
    return f"url {state.url!r} with {len(state.elements)} elements, e.g. {sample}"
