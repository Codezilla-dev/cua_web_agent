"""CLI entry point: `python -m src.cli discover ...`.

Deliberately thin: parse arguments, construct objects, print a result. No
decisions of its own.
"""

import argparse
import getpass
import sys
from collections.abc import Mapping

import httpx

from src.agent.loop import Budget, run_discovery
from src.agent.planner import Planner
from src.artifact import (
    ArtifactError,
    CompilerError,
    artifact_path,
    compile_capability,
    generalize,
    recorded_urls,
)
from src.artifact import dump as dump_capability
from src.artifact import load as load_capability
from src.artifact.models import Capability
from src.artifact.semantics import distil
from src.config import Settings, load_settings, require_llm
from src.escalation import (
    RETRY_STEP,
    STEP_ALREADY_PERFORMED,
    Intervention,
    pending_interventions,
)
from src.evidence import EvidenceWriter, generate_run_id
from src.policy import Policy, Redactor
from src.replay import (
    BusinessOutcome,
    EscalationPolicy,
    Recovery,
    ReplayEvidenceWriter,
    ReplayRequest,
    ReplayResult,
    Success,
    replay,
)
from src.session.lease import UNCLAIMED, LeaseConflictError, SessionControl
from src.surface.web import WebSurface
from src.types import RunOutcome, Trace


def main() -> None:
    args = _parse_args()

    if args.command == "discover":
        trace = _run_discover(args, load_settings())
        sys.exit(0 if trace.outcome == RunOutcome.COMPLETED else 1)

    if args.command == "replay":
        sys.exit(_run_replay(args, load_settings()))

    if args.command == "operator":
        sys.exit(_run_operator(args))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cua", description="Computer-use capability system: drive a live UI toward a goal."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    discover = subparsers.add_parser(
        "discover", help="Run one discovery loop toward a goal, starting at --target."
    )
    discover.add_argument(
        "--goal", required=True, help="Natural-language goal for this run, e.g. "
        '"look up member 12345 and read their savings balance".'
    )
    discover.add_argument(
        "--target", required=True, help="URL to start the run at, e.g. http://localhost:5000."
    )
    discover.add_argument(
        "--max-steps", type=int, default=None, help="Override run.max_steps from config."
    )
    discover.add_argument(
        "--max-seconds", type=float, default=None, help="Override run.max_seconds from config."
    )
    headed_group = discover.add_mutually_exclusive_group()
    headed_group.add_argument(
        "--headed", dest="headed", action="store_true", default=None,
        help="Show the browser window (overrides CUA_HEADED).",
    )
    headed_group.add_argument(
        "--headless", dest="headed", action="store_false", default=None,
        help="Run without a visible window (overrides CUA_HEADED).",
    )
    discover.add_argument(
        "--id", dest="capability_id", default=None, metavar="ID",
        help=(
            "Name the compiled capability instead of deriving it from the goal. "
            "The derived id drops the goal's argument values, so two goals that "
            "differ only in the record they name compile to the same id and the "
            "second overwrites the first -- which is correct, and is why this "
            "override exists."
        ),
    )
    discover.add_argument(
        "--model", default=None, help="Override the configured model id for this run."
    )

    replay_parser = subparsers.add_parser(
        "replay", help="Re-run a recorded capability deterministically, with no model."
    )
    replay_parser.add_argument("capability", help="Capability id, e.g. look_up_member_read.")
    replay_parser.add_argument(
        "--param", action="append", default=[], metavar="NAME=VALUE",
        help="An input for this invocation. Repeatable.",
    )
    replay_headed = replay_parser.add_mutually_exclusive_group()
    replay_headed.add_argument(
        "--headed", dest="headed", action="store_true", default=None,
        help="Show the browser window (overrides CUA_HEADED).",
    )
    replay_headed.add_argument(
        "--headless", dest="headed", action="store_false", default=None,
        help="Run without a visible window (overrides CUA_HEADED).",
    )
    replay_parser.add_argument(
        "--escalate", action="store_true",
        help=(
            "On a failure a person could fix, hand this live session to an operator "
            "and wait. Implies --headed: a human cannot drive a headless browser."
        ),
    )
    replay_parser.add_argument(
        "--operator-port", type=int, default=9222, metavar="PORT",
        help="CDP port the operator console attaches to during a handoff.",
    )
    replay_parser.add_argument(
        "--escalate-timeout", type=float, default=900.0, metavar="SECONDS",
        help="How long to wait for an operator before giving up (default 900).",
    )

    operator_parser = subparsers.add_parser(
        "operator",
        help="Take control of a stuck session, and hand it back.",
    )
    operator_sub = operator_parser.add_subparsers(dest="operator_command", required=True)

    operator_sub.add_parser("list", help="Interventions still waiting on a person.")

    take = operator_sub.add_parser(
        "take", help="Claim the live session for a human. The run stops acting."
    )
    take.add_argument("intervention_id")
    take.add_argument(
        "--as", dest="holder_id", default=None,
        help="Who is taking it. Defaults to the OS user.",
    )

    resume = operator_sub.add_parser(
        "resume", help="Hand the session back. The run continues from the next step."
    )
    resume.add_argument("intervention_id")
    resume.add_argument("--as", dest="holder_id", default=None)
    resume.add_argument(
        "--performed", action="store_true",
        help=(
            "You carried out the stuck step by hand. The run will move on rather "
            "than repeat it -- which for a submit or a transfer matters."
        ),
    )

    status = operator_sub.add_parser("status", help="Who holds a session right now.")
    status.add_argument("intervention_id")

    return parser.parse_args()


def _run_operator(args: argparse.Namespace) -> int:
    """The operator console, as a CLI.

    The brief permits mocking the operator UI, and this is that mock -- but the
    mechanism underneath is real: `take` and `resume` move a lease that a
    running process is actually blocked on, and the human works in the headed
    browser window the run already opened. What is absent is chrome, not
    mechanism.
    """
    holder_id = getattr(args, "holder_id", None) or getpass.getuser()

    if args.operator_command == "list":
        pending = pending_interventions()
        if not pending:
            print("no interventions are waiting")
            return 0
        for intervention in pending:
            request = intervention.read_request()
            lease = intervention.lease.read()
            claimed = "unclaimed" if lease.holder_id == UNCLAIMED else f"held by {lease.holder_id}"
            print(f"{request.intervention_id}  {request.condition.value}  ({claimed})")
            print(f"    step {request.step_index}: {request.step_intent}")
            print(f"    at   {request.url or '(no url recorded)'}")
            print(f"    why  {request.detail or request.guidance}")
        return 0

    intervention = Intervention(args.intervention_id)
    if not intervention.request_path.exists():
        print(f"operator: no intervention {args.intervention_id!r}")
        return 1

    if args.operator_command == "status":
        lease = intervention.lease.read()
        print(f"{args.intervention_id}: {lease.owner.value} ({lease.holder_id})")
        print(f"  since  {lease.since.isoformat()}")
        print(f"  reason {lease.reason or '(none recorded)'}")
        return 0

    if args.operator_command == "take":
        request = intervention.read_request()
        try:
            intervention.take(holder_id)
        except LeaseConflictError as error:
            print(f"operator: {error}")
            return 1
        print(f"{holder_id} now has control of {args.intervention_id}.")
        print(f"  {request.guidance}")
        print("  The session is the browser window the run already opened; it is at")
        print(f"  {request.url or 'an unrecorded url'}.")
        print(f"  When you are done: cua operator resume {args.intervention_id}")
        return 0

    if args.operator_command == "resume":
        disposition = STEP_ALREADY_PERFORMED if args.performed else RETRY_STEP
        intervention.resume(holder_id, step_disposition=disposition)
        print(f"step {intervention.read_request().step_index} will be {disposition}.")
        print(
            "control returned to automation; the run continues from step "
            f"{intervention.read_request().step_index}."
        )
        return 0

    return 1


def _run_discover(args: argparse.Namespace, settings: Settings) -> Trace:
    """Build every Phase 1 piece from `settings` (with `args` overrides
    applied) and drive one discovery run to completion."""
    require_llm(settings)
    model = args.model or settings.llm.model
    headed = settings.headed if args.headed is None else args.headed
    max_steps = settings.run.max_steps if args.max_steps is None else args.max_steps
    max_seconds = settings.run.max_seconds if args.max_seconds is None else args.max_seconds

    run_id = generate_run_id()
    redactor = Redactor(
        sensitive_name_pattern=settings.redaction.sensitive_name_pattern,
        replacement=settings.redaction.replacement,
        # The password is the one value we know is secret before the run starts,
        # so it is swept out of every string that reaches disk. The username is
        # deliberately not swept: it is not a secret, and scrubbing it would make
        # the trace much harder to read.
        secret_values=[settings.target.password] if settings.target.password else [],
    )
    evidence = EvidenceWriter(run_id=run_id, redactor=redactor)

    surface = WebSurface(
        headed=headed,
        settle_timeout_s=settings.run.settle_timeout_s,
        min_interactive_elements=settings.perception.min_interactive_elements,
        max_unnamed_ratio=settings.perception.max_unnamed_ratio,
    )
    planner = Planner(
        api_key=settings.llm.api_key,
        base_url=settings.llm.base_url,
        model=model,
        # --model replaces the primary only; fallbacks still apply under it.
        fallback_models=settings.llm.fallback_models,
        enable_model_routing=settings.llm.supports_model_routing,
        username=settings.target.username,
        password=settings.target.password,
    )
    policy = Policy.from_settings(settings)
    budget = Budget(max_steps=max_steps, max_seconds=max_seconds)

    print(f"discover: run_id={run_id} goal={args.goal!r} target={args.target!r}")
    try:
        trace = run_discovery(
            goal=args.goal,
            target_url=args.target,
            surface=surface,
            planner=planner,
            policy=policy,
            evidence=evidence,
            budget=budget,
        )
    finally:
        # run_discovery owns surface.stop(); the HTTP client is ours.
        planner.close()

    # Reads evidence_dir directly rather than rebuilding the path.
    outcome_label = trace.outcome.value if trace.outcome is not None else "unknown"
    print(
        f"outcome: {outcome_label} | steps: {len(trace.steps)} | "
        f"run_id: {run_id} | evidence: {evidence.evidence_dir}"
    )
    if trace.error:
        print(f"error: {trace.error}")

    _compile_artifact(trace, settings, getattr(args, "capability_id", None))
    return trace


def _compile_artifact(
    trace: Trace, settings: Settings, capability_id: str | None = None
) -> None:
    """Compile a completed run into a capability artifact.

    Compilation runs against the in-memory trace, which still holds the real
    typed values -- the copy on disk is redacted, and a capability compiled from
    that would carry `***REDACTED***` where a parameter belongs. The credentials
    are handed over so the compiler can recognise them by value and replace them
    with a reference instead of writing them into the artifact.

    A failed run is not an error here: it simply has no capability to compile,
    and the trace is still worth keeping.
    """
    try:
        capability = compile_capability(
            trace,
            capability_id=capability_id,
            credentials={
                "username": settings.target.username or "",
                "password": settings.target.password or "",
            },
        )
    except CompilerError as error:
        print(f"artifact: not compiled ({error})")
        return

    capability = _generalize(capability, settings, *recorded_urls(trace))

    path = dump_capability(capability)
    print(
        f"artifact: {path} | id={capability.id} "
        f"| steps={len(capability.steps)} "
        f"| inputs={[p.name for p in capability.inputs]} "
        f"| outputs={[o.name for o in capability.outputs]}"
    )


def _generalize(
    capability: Capability,
    settings: Settings,
    step_urls: Mapping[int, str],
    final_url: str,
) -> Capability:
    """Ask a model which expectations quote the recorded record, and weaken those.

    The compiler is deterministic and stays that way; this is the one judgement
    it cannot make, because "is this string page furniture or a datum" has no
    syntactic signature. See `src/artifact/semantics.py` for the full argument.

    Every failure here is a no-op by construction -- no key, no network, a
    malformed response, a label about a step that does not exist -- so the
    artifact is never worse for having tried. That is why there is no error
    branch: `distil` returns `None` and the compiled capability stands.
    """
    if not settings.llm.api_key:
        return capability

    with httpx.Client(
        headers={"Authorization": f"Bearer {settings.llm.api_key}"}
    ) as client:
        distillation = distil(
            capability,
            client=client,
            model=settings.llm.model,
            url=f"{settings.llm.base_url.rstrip('/')}/chat/completions",
        )

    if distillation is None:
        print("artifact: semantic pass unavailable; keeping the compiler's expectations")
        return capability

    generalized, notes = generalize.apply(capability, distillation, step_urls, final_url)
    for note in notes:
        print(f"artifact: {note}")
    if not notes:
        print("artifact: every expectation already structural")
    return generalized


# --------------------------------------------------------------------------- #
# replay
# --------------------------------------------------------------------------- #
# Exit codes carry the distinction the result type makes. A business outcome is
# a real answer, so it is not a crash -- but it is not success either, and a
# caller scripting this needs to tell the three apart without parsing stdout.
EXIT_SUCCESS = 0
EXIT_BUSINESS_OUTCOME = 2
EXIT_HARD_FAILURE = 1


def _run_replay(args: argparse.Namespace, settings: Settings) -> int:
    """Replay a saved capability and print its typed result."""
    try:
        capability = load_capability(artifact_path(args.capability))
    except ArtifactError as error:
        print(f"replay: {error}")
        return EXIT_HARD_FAILURE

    try:
        params = dict(pair.split("=", 1) for pair in args.param)
    except ValueError:
        print("replay: each --param must be NAME=VALUE")
        return EXIT_HARD_FAILURE

    headed = settings.headed if args.headed is None else args.headed
    # A handoff means a person operating this window. Offering them a headless
    # one would be offering them nothing, so the flag settles it rather than
    # letting the two options contradict each other silently.
    if args.escalate:
        headed = True

    control = SessionControl()
    surface = WebSurface(
        headed=headed,
        settle_timeout_s=settings.run.settle_timeout_s,
        min_interactive_elements=settings.perception.min_interactive_elements,
        max_unnamed_ratio=settings.perception.max_unnamed_ratio,
        control=control,
        debug_port=args.operator_port if args.escalate else None,
    )

    # A replay leaves the same trail a discovery run does. The redactor is built
    # identically -- a replay binds the real password into a real action, so it
    # has exactly as much opportunity to leak one.
    run_id = generate_run_id()
    redactor = Redactor(
        sensitive_name_pattern=settings.redaction.sensitive_name_pattern,
        replacement=settings.redaction.replacement,
        secret_values=[settings.target.password] if settings.target.password else [],
    )
    # Values the artifact declares as personal data. Masked in the bundle by
    # declaration rather than by pattern: a member id looks like any other
    # number, so the only honest signal is the capability author saying so.
    evidence = ReplayEvidenceWriter(
        run_id=run_id,
        redactor=redactor,
        masked_values=[
            value
            for spec in capability.inputs
            if spec.sensitivity in ("pii", "secret")
            # The caller's value *and* the recorded one. The step intents the
            # planner wrote quote the example ("Type the member ID '12345'..."),
            # so masking only the caller's value leaves a real identifier
            # readable in the prose of every bundle. The artifact itself still
            # carries `example` in plaintext -- declaring a parameter sensitive
            # does not retroactively sanitise the capability file.
            for value in (params.get(spec.name), spec.example)
            if value
        ],
    )
    escalation = EscalationPolicy(
        run_id=run_id,
        control=control,
        redactor=redactor,
        wait_timeout_s=args.escalate_timeout,
        enabled=args.escalate,
    )

    print(f"replay: capability={capability.id!r} params={params}")
    result = replay(
        ReplayRequest(
            capability=capability,
            params=params,
            surface=surface,
            policy=Policy.from_settings(settings),
            credentials={
                "username": settings.target.username or "",
                "password": settings.target.password or "",
            },
            business_rules=list(settings.replay.business_outcomes),
            settle_timeout_s=settings.run.settle_timeout_s,
            evidence=evidence,
            escalation=escalation,
        )
    )
    print(f"evidence: {evidence.evidence_dir}")
    return _report_replay(result)


def _report_replay(result: ReplayResult) -> int:
    """Print the result in the shape its type implies, and map it to an exit code."""
    if isinstance(result, Success):
        print(f"result: success | steps={result.steps_run}")
        for name, value in result.outputs.items():
            print(f"  {name} = {value!r}")
        _report_recoveries(result.recoveries)
        return EXIT_SUCCESS

    if isinstance(result, BusinessOutcome):
        print(f"result: business outcome | {result.code} at step {result.step_index}")
        print(f"  {result.detail}")
        _report_recoveries(result.recoveries)
        return EXIT_BUSINESS_OUTCOME

    print(f"result: hard failure | {result.kind} at step {result.step_index}")
    if result.step_intent:
        print(f"  step     : {result.step_intent}")
    print(f"  expected : {result.expected}")
    print(f"  observed : {result.observed}")
    _report_recoveries(result.recoveries)
    return EXIT_HARD_FAILURE


def _report_recoveries(recoveries: list[Recovery]) -> None:
    """Anything absorbed on the way is reported, never silently swallowed."""
    for recovery in recoveries:
        print(
            f"  recovered: step {recovery.step_index} {recovery.condition} "
            f"-> {recovery.action_taken}"
        )


if __name__ == "__main__":
    main()
