"""Proof-of-mechanics run for `src/agent/loop.py`, with zero LLM calls.

A hand-scripted stub `Planner` reads the live `UIState` and returns each
`Intent` itself; policy and evidence are stubbed inline. It gives a wrong
target_ref the first time it clicks "Sign In", so the run exercises the full
recovery path against the real browser and target_app:

    original attempt (bogus ref) -> TargetNotFoundError -> VERIFY_FAILED
    retry (same bogus ref again) -> TargetNotFoundError -> VERIFY_FAILED
    replan (fresh decide, correct ref) -> succeeds -> REPLANNED

Everything after that (search in the iframe, open the record, read the
balance, declare done) runs the ordinary happy path.

Run this against a running target_app (`uv run python -m target_app` in
another terminal), or pass --start-target-app to have this script start and
stop one itself.

Usage:
    uv run python scripts/loop_dry_run.py
    uv run python scripts/loop_dry_run.py --headed --start-target-app
"""

from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.agent.loop import Budget, run_discovery  # noqa: E402
from src.surface.web import WebSurface  # noqa: E402
from src.types import (  # noqa: E402
    ActionKind,
    Decision,
    ElementVisibleCheck,
    Expectation,
    FieldHasValueCheck,
    Intent,
    PolicyVerdict,
    RiskTier,
    RunOutcome,
    StepOutcome,
    StepRecord,
    TextVisibleCheck,
    Trace,
    UIElement,
    UIState,
    UrlContainsCheck,
)
from target_app.data import OPERATOR_PASSWORD, OPERATOR_USERNAME  # noqa: E402

TARGET_HOST = "127.0.0.1"
TARGET_PORT = 5000
TARGET_URL = f"http://{TARGET_HOST}:{TARGET_PORT}"
GOAL = "look up member 12345 and read their savings balance"
BOOT_TIMEOUT_S = 10.0

# A ref no real snapshot will ever assign, used to force the first "click
# Sign In" attempt to fail with TargetNotFoundError and prove recovery fires
# against the real loop, not just in theory.
DELIBERATELY_WRONG_REF = "e404"


class LookupFailure(Exception):
    """Raised when the stub planner can't find an element it expects on the
    live page. A bug in the hand-scripted phases should be loud, not
    silently produce a nonsensical Intent."""


def _find(state: UIState, role: str, name_contains: str) -> UIElement:
    needle = name_contains.casefold()
    for element in state.elements:
        if element.role == role and needle in element.name.casefold():
            return element
    raise LookupFailure(
        f"no {role!r} element with name containing {name_contains!r} "
        f"(url={state.url!r}, {len(state.elements)} elements)"
    )


def _find_by_value(state: UIState, needle: str) -> UIElement:
    for element in state.elements:
        if needle in (element.value or ""):
            return element
    raise LookupFailure(f"no element with value containing {needle!r} (url={state.url!r})")


class StubPlanner:
    """Hand-scripted stand-in for `planner.py`'s real `Planner`.

    Satisfies the `loop.Planner` protocol structurally -- a `decide` method
    with the right signature, nothing more. Each call inspects the *current*
    live state and returns whatever step is still unmet, so it behaves
    correctly whether it's being called for the first time or as a replan.
    """

    def __init__(self) -> None:
        self._password_typed = False
        self._balance_read = False

    def decide(
        self, goal: str, state: UIState, history: list[str], last_failure: str | None
    ) -> Decision:
        started = time.monotonic()
        intent = self._next_intent(state, last_failure)
        return Decision(
            intent=intent,
            model="stub-scripted-planner",
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    def _next_intent(self, state: UIState, last_failure: str | None) -> Intent:
        if "/login" in state.url:
            return self._login_step(state, last_failure)
        if "/member/" in state.url:
            return self._member_step(state)
        if "/search" in state.url:
            return self._search_step(state)
        raise LookupFailure(f"stub planner has no phase for url {state.url!r}")

    # -- phases, most-progressed condition checked first, so re-entering a
    #    phase (as a replan does) always picks up where reality actually is --

    def _login_step(self, state: UIState, last_failure: str | None) -> Intent:
        username = _find(state, "textbox", "operator id")
        if (username.value or "") != OPERATOR_USERNAME:
            return Intent(
                reasoning="Operator ID is empty; type the known username.",
                intent="type operator id",
                action=ActionKind.TYPE,
                target_ref=username.ref,
                target_description="Operator ID textbox",
                value=OPERATOR_USERNAME,
                risk=RiskTier.SAFE,
                expectation=Expectation(
                    description=f"Operator ID field holds {OPERATOR_USERNAME!r}",
                    check=FieldHasValueCheck(
                        name_contains="Operator ID", expected=OPERATOR_USERNAME
                    ),
                ),
            )

        if not self._password_typed:
            password = _find(state, "textbox", "passcode")
            self._password_typed = True
            return Intent(
                reasoning="Operator ID is filled; type the known passcode.",
                intent="type passcode",
                action=ActionKind.TYPE,
                target_ref=password.ref,
                target_description="Passcode textbox",
                value=OPERATOR_PASSWORD,
                risk=RiskTier.SAFE,
                # Perception never reports a password field's value (it is
                # blanked at the source, before redaction even runs), so
                # verifying by value is impossible here. Confirming the field
                # is still present is the honest check available.
                expectation=Expectation(
                    description="Passcode field is still present after typing into it",
                    check=ElementVisibleCheck(role="textbox", name_contains="Passcode"),
                ),
            )

        expectation = Expectation(
            description="signing in redirects to /search",
            check=UrlContainsCheck(fragment="/search"),
        )
        if last_failure is None:
            # Deliberately wrong: proves retry (same bad ref) then replan
            # (a fresh decide() finds the real button) both work end to end.
            return Intent(
                reasoning="(proof run) deliberately targeting a ref that does not "
                "exist, to exercise retry-then-replan recovery.",
                intent="click sign in (deliberately wrong ref)",
                action=ActionKind.CLICK,
                target_ref=DELIBERATELY_WRONG_REF,
                target_description="Sign In button",
                risk=RiskTier.CONSEQUENTIAL,
                expectation=expectation,
            )
        sign_in = _find(state, "button", "sign in")
        return Intent(
            reasoning="Both fields are filled; submit the login form.",
            intent="click sign in",
            action=ActionKind.CLICK,
            target_ref=sign_in.ref,
            target_description="Sign In button",
            risk=RiskTier.CONSEQUENTIAL,
            expectation=expectation,
        )

    def _search_step(self, state: UIState) -> Intent:
        open_record_links = [
            element
            for element in state.elements
            if element.role == "link" and "open record" in element.name.casefold()
        ]
        if open_record_links:
            link = open_record_links[0]
            return Intent(
                reasoning="A matching member row is showing; open its record.",
                intent="open member record",
                action=ActionKind.CLICK,
                target_ref=link.ref,
                target_description="Open Record link",
                risk=RiskTier.CONSEQUENTIAL,
                expectation=Expectation(
                    description="opening the record navigates to /member/12345",
                    check=UrlContainsCheck(fragment="/member/12345"),
                ),
            )

        query_field = _find(state, "textbox", "member id or name")
        if (query_field.value or "") != "12345":
            return Intent(
                reasoning="Search field is empty; type the member id to look up.",
                intent="type search query",
                action=ActionKind.TYPE,
                target_ref=query_field.ref,
                target_description="Member ID or Name textbox",
                value="12345",
                risk=RiskTier.SAFE,
                expectation=Expectation(
                    description="the search field holds '12345'",
                    check=FieldHasValueCheck(name_contains="Member ID or Name", expected="12345"),
                ),
            )

        find_button = _find(state, "button", "find member")
        return Intent(
            reasoning="Query is typed; submit the search.",
            intent="submit search",
            action=ActionKind.CLICK,
            target_ref=find_button.ref,
            target_description="Find Member button",
            risk=RiskTier.CONSEQUENTIAL,
            expectation=Expectation(
                description="a result row with an Open Record link appears",
                check=TextVisibleCheck(text="Open Record"),
            ),
        )

    def _member_step(self, state: UIState) -> Intent:
        if not self._balance_read:
            balance = _find_by_value(state, "4,812.55")
            self._balance_read = True
            return Intent(
                reasoning="On the member record; read the savings balance.",
                intent="read savings balance",
                action=ActionKind.READ,
                target_ref=balance.ref,
                target_description="Current Balance for the Savings account",
                risk=RiskTier.SAFE,
                expectation=Expectation(
                    description="the balance $4,812.55 is visible",
                    check=TextVisibleCheck(text="4,812.55"),
                ),
            )

        return Intent(
            reasoning="Savings balance has been read; the goal is complete.",
            intent="declare goal complete",
            action=ActionKind.DONE,
            risk=RiskTier.SAFE,
            expectation=Expectation(
                description="the balance $4,812.55 is visible",
                check=TextVisibleCheck(text="4,812.55"),
            ),
        )


class AllowAllPolicy:
    """Stand-in for `policy/`. Always allows -- proving what the guard stage
    *does* with a verdict is `loop.py`'s job, not policy's, so the stub does
    not need real allowlist/risk logic to prove the loop mechanics."""

    def check(self, intent: Intent, state: UIState, target_url: str) -> PolicyVerdict:
        return PolicyVerdict(
            allowed=True,
            tier=intent.risk,
            rule="stub-allow-all",
            reason="loop dry run: policy is stubbed to always allow",
            disposition="allow",
        )


class FileEvidenceStub:
    """Stand-in for `evidence.py`. Writes the same file layout the real
    `EvidenceWriter` will (trace.jsonl, run.json, per-step PNGs) so this run
    produces real, inspectable evidence, without redaction (that is
    `policy.redact_step`'s job, not built yet)."""

    def __init__(self, root: Path) -> None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        self.run_id = f"{stamp}-dryrun"
        self._dir = root / self.run_id
        self._dir.mkdir(parents=True, exist_ok=True)
        self._trace_path = self._dir / "trace.jsonl"

    def step_png_path(self, index: int) -> str:
        return str(self._dir / f"step_{index:02d}.png")

    def append_step(self, step: StepRecord) -> None:
        with self._trace_path.open("a", encoding="utf-8") as handle:
            handle.write(step.model_dump_json() + "\n")

    def write_run(self, trace: Trace) -> None:
        run_path = self._dir / "run.json"
        run_path.write_text(
            trace.model_dump_json(exclude={"steps"}, indent=2), encoding="utf-8"
        )


def _port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    parser.add_argument(
        "--start-target-app",
        action="store_true",
        help="start target_app for this run and stop it after",
    )
    args = parser.parse_args()

    target_app_process: subprocess.Popen | None = None
    if args.start_target_app:
        if _port_open(TARGET_HOST, TARGET_PORT):
            print(f"[SKIP] --start-target-app given, but :{TARGET_PORT} is already up; reusing it")
        else:
            target_app_process = subprocess.Popen(
                [sys.executable, "-m", "target_app"],
                cwd=REPO_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            deadline = time.monotonic() + BOOT_TIMEOUT_S
            while time.monotonic() < deadline and not _port_open(TARGET_HOST, TARGET_PORT):
                time.sleep(0.2)
    if not _port_open(TARGET_HOST, TARGET_PORT):
        print(f"[FAIL] nothing is answering on {TARGET_URL}.")
        print("       Start target_app first, or pass --start-target-app.")
        return 1

    surface = WebSurface(headed=args.headed, min_interactive_elements=1)
    planner = StubPlanner()
    policy = AllowAllPolicy()
    evidence = FileEvidenceStub(REPO_ROOT / "runs" / "loop-dry-run")
    budget = Budget(max_steps=20, max_seconds=120.0)

    print(f"=== running the discovery loop against a stub planner (evidence: {evidence._dir}) ===")
    try:
        trace = run_discovery(
            goal=GOAL,
            target_url=f"{TARGET_URL}/login",
            surface=surface,
            planner=planner,
            policy=policy,
            evidence=evidence,
            budget=budget,
        )
    finally:
        if target_app_process is not None:
            target_app_process.terminate()
            try:
                target_app_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                target_app_process.kill()

    print(f"\noutcome: {trace.outcome}")
    print(f"steps recorded: {len(trace.steps)}")
    for step in trace.steps:
        intent = step.decision.intent
        note = f" [{step.recovery_note}]" if step.recovery_note else ""
        read = f" read_value={step.act.read_value!r}" if step.act and step.act.read_value else ""
        print(f"  step {step.index}: {intent.action.value} -> {step.outcome.value}{note}{read}")

    # detail is `object`, not `str`: each check's detail is only ever
    # formatted for printing on failure, and the values below are naturally a
    # mix of strings and lists.
    checks: list[tuple[str, bool, object]] = []

    checks.append(("run completed", trace.outcome == RunOutcome.COMPLETED, str(trace.outcome)))

    outcomes = [step.outcome for step in trace.steps]
    recovery_fired = (
        outcomes.count(StepOutcome.VERIFY_FAILED) >= 2 and StepOutcome.REPLANNED in outcomes
    )
    checks.append(
        (
            "recovery path fired (2x VERIFY_FAILED then a REPLANNED, for the deliberately bad ref)",
            recovery_fired,
            f"outcomes seen: {[o.value for o in outcomes]}",
        )
    )

    read_steps = [step for step in trace.steps if step.decision.intent.action == ActionKind.READ]
    balance_read = any(
        step.act is not None
        and step.act.read_value is not None
        and "4,812.55" in step.act.read_value
        for step in read_steps
    )
    checks.append(
        (
            "a READ step's read_value holds $4,812.55",
            balance_read,
            [s.act.read_value for s in read_steps if s.act] or ["<no READ steps>"],
        )
    )

    evidence_files = sorted(p.name for p in evidence._dir.iterdir())
    has_trace = "trace.jsonl" in evidence_files
    has_run = "run.json" in evidence_files
    has_pngs = any(name.endswith(".png") for name in evidence_files)
    checks.append(
        (
            "evidence directory has trace.jsonl, run.json, and step PNGs",
            has_trace and has_run and has_pngs,
            evidence_files,
        )
    )

    print("\n=== checks ===")
    all_ok = True
    for name, ok, detail in checks:
        marker = "[OK]  " if ok else "[FAIL]"
        print(f"{marker} {name}" + (f" -- {detail}" if not ok else ""))
        all_ok = all_ok and ok

    if trace.error:
        print(f"\ntrace.error: {trace.error}")

    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
