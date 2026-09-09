"""One real call to the configured model, to catch an integration problem
(request shape, forced tool call, response shape) against a live perceived page.

Perceives `target_app`'s login page and asks the planner for the next step,
using `target_app`'s own credentials rather than `settings.target.*`.

Usage:
    uv run python scripts/planner_live_check.py
    uv run python scripts/planner_live_check.py --start-target-app
    uv run python scripts/planner_live_check.py --headed
"""

from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.agent.planner import Planner  # noqa: E402
from src.config import load_settings  # noqa: E402
from src.surface.web import WebSurface  # noqa: E402
from target_app.data import OPERATOR_PASSWORD, OPERATOR_USERNAME  # noqa: E402

TARGET_HOST = "127.0.0.1"
TARGET_PORT = 5000
TARGET_URL = f"http://{TARGET_HOST}:{TARGET_PORT}"
GOAL = "look up member 12345 and read their savings balance"
BOOT_TIMEOUT_S = 10.0

# Imported from the fixture rather than restated. These three scripts each
# held their own copy of the passcode, and when the fixture's passcode changed
# every one of them started failing at the login step -- Layer 3 of the
# verification stack, silently broken by a one-line change somewhere else.
TARGET_APP_USERNAME = OPERATOR_USERNAME
TARGET_APP_PASSWORD = OPERATOR_PASSWORD


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
    parser.add_argument(
        "--model",
        default=None,
        help="override settings.llm.model for this run only",
    )
    args = parser.parse_args()

    settings = load_settings()
    model = args.model or settings.llm.model

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
    planner = Planner(
        api_key=settings.llm.api_key,
        base_url=settings.llm.base_url,
        model=model,
        username=TARGET_APP_USERNAME,
        password=TARGET_APP_PASSWORD,
    )

    try:
        surface.start()
        surface.goto(f"{TARGET_URL}/login")
        state = surface.perception.perceive()
        print(
            f"perceived {len(state.elements)} elements at {state.url!r} "
            f"(coverage ok={state.coverage.ok})"
        )

        print(f"\ncalling {model} via {settings.llm.base_url} ...")
        decision = planner.decide(goal=GOAL, state=state, history=[], last_failure=None)

        print(
            f"\nlatency_ms={decision.latency_ms} "
            f"prompt_tokens={decision.prompt_tokens} "
            f"completion_tokens={decision.completion_tokens}"
        )
        print(f"raw_response_id={decision.raw_response_id}")
        print()
        print(decision.intent.model_dump_json(indent=2))

        intent = decision.intent
        known_refs = {element.ref for element in state.elements}
        checks: list[tuple[str, bool, object]] = [
            ("reasoning is non-empty", bool(intent.reasoning.strip()), intent.reasoning[:80]),
            ("expectation has a check kind", True, intent.expectation.check.kind),
        ]
        if intent.target_ref is not None:
            checks.append(
                (
                    "target_ref (given) is a real ref from the perceived table",
                    intent.target_ref in known_refs,
                    intent.target_ref,
                )
            )

        print("\n=== checks ===")
        failed = False
        for name, ok, detail in checks:
            marker = "[OK]  " if ok else "[FAIL]"
            print(f"{marker} {name} -- {detail}")
            failed = failed or not ok
        return 1 if failed else 0
    finally:
        planner.close()
        surface.stop()
        if target_app_process is not None:
            target_app_process.terminate()
            try:
                target_app_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                target_app_process.kill()


if __name__ == "__main__":
    raise SystemExit(main())
