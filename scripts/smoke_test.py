"""Checks the pieces run together: config parses, `target_app` serves, and
`WebSurface` drives a real headless browser through login -> perceive -> read.
No LLM or `.env` needed; exits non-zero on any failure.

Usage:
    uv run python scripts/smoke_test.py
    uv run python scripts/smoke_test.py --headed         # watch the browser
    uv run python scripts/smoke_test.py --skip-static     # skip ruff/pyright/pytest
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

from target_app.data import OPERATOR_PASSWORD, OPERATOR_USERNAME  # noqa: E402

TARGET_HOST = "127.0.0.1"
TARGET_PORT = 5000
TARGET_URL = f"http://{TARGET_HOST}:{TARGET_PORT}"
BOOT_TIMEOUT_S = 10.0

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
_MARKERS = {PASS: "[OK]  ", FAIL: "[FAIL]", SKIP: "[SKIP]"}

results: list[tuple[str, str, str]] = []  # (check name, status, detail)


def record(name: str, status: str, detail: str = "") -> None:
    results.append((name, status, detail))
    line = f"{_MARKERS[status]} {name}"
    if detail:
        line += f" — {detail}"
    print(line)


def run_and_capture(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)


# --------------------------------------------------------------------------- #
# 1. Static checks: the same three commands README.md names as the quality gate.
# --------------------------------------------------------------------------- #
def check_static() -> None:
    for label, cmd in [
        ("static: ruff check .", ["uv", "run", "ruff", "check", "."]),
        ("static: pyright", ["uv", "run", "pyright"]),
        ("static: pytest", ["uv", "run", "pytest", "-q"]),
    ]:
        proc = run_and_capture(cmd)
        if proc.returncode == 0:
            record(label, PASS)
        else:
            tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-8:])
            record(label, FAIL, tail)


# --------------------------------------------------------------------------- #
# 2. Config: does config/default.yaml still satisfy the typed Settings models?
#    Skips the env-dependent half of load_settings() (OPENROUTER_API_KEY etc.)
#    since that requires real credentials this script has no business needing.
# --------------------------------------------------------------------------- #
def check_config() -> None:
    try:
        import yaml

        from src.config import (
            DEFAULT_CONFIG_PATH,
            AllowlistSettings,
            PerceptionSettings,
            RedactionSettings,
            RiskPolicySettings,
            RunSettings,
        )

        raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
        AllowlistSettings(**raw["allowlist"])
        RiskPolicySettings(**raw["risk_policy"])
        RedactionSettings(**raw["redaction"])
        RunSettings(**raw["run"])
        PerceptionSettings(**raw["perception"])
        record("config: config/default.yaml matches the Settings models", PASS)
    except Exception as error:
        record("config: config/default.yaml matches the Settings models", FAIL, str(error))


# --------------------------------------------------------------------------- #
# 3. target_app: does it boot and actually serve a page?
# --------------------------------------------------------------------------- #
def _port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def start_target_app() -> subprocess.Popen:
    # sys.executable, not `uv run`, so `.terminate()` kills exactly one process
    # instead of an `uv` wrapper that may leave the real server orphaned.
    return subprocess.Popen(
        [sys.executable, "-m", "target_app"],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def check_target_app_http(started_by_this_run: bool) -> bool:
    import httpx

    if started_by_this_run:
        deadline = time.monotonic() + BOOT_TIMEOUT_S
        while time.monotonic() < deadline and not _port_open(TARGET_HOST, TARGET_PORT):
            time.sleep(0.2)
        if not _port_open(TARGET_HOST, TARGET_PORT):
            record("target_app: boots and accepts connections", FAIL, "port never opened")
            return False
        record("target_app: boots and accepts connections", PASS)

    try:
        response = httpx.get(f"{TARGET_URL}/login", follow_redirects=True, timeout=5.0)
        ok = response.status_code == 200 and "Operator Sign In" in response.text
        record(
            "target_app: GET /login renders the sign-in page",
            PASS if ok else FAIL,
            "" if ok else f"status={response.status_code}",
        )
        return ok
    except Exception as error:
        record("target_app: GET /login renders the sign-in page", FAIL, str(error))
        return False


# --------------------------------------------------------------------------- #
# 4. WebSurface: perception, action execution, settle, and a READ all
#    exercised together against the live target_app, exactly like one
#    iteration of the future discovery loop would use them.
# --------------------------------------------------------------------------- #
def check_web_surface(headed: bool) -> None:
    from src.surface.web import WebSurface
    from src.types import (
        ActionKind,
        ActResult,
        Expectation,
        Intent,
        RiskTier,
        TextVisibleCheck,
        UIState,
    )

    def do(action: ActionKind, ref: str | None, value: str | None, state: UIState) -> ActResult:
        target = surface.perception.resolve(ref, state) if ref else None
        intent = Intent(
            reasoning="smoke test",
            intent="smoke test step",
            action=action,
            target_ref=ref,
            value=value,
            risk=RiskTier.SAFE,
            expectation=Expectation(description="n/a", check=TextVisibleCheck(text="n/a")),
        )
        return surface.actions.execute(intent, target)

    surface = WebSurface(headed=headed, min_interactive_elements=1)
    try:
        surface.start()
        surface.goto(f"{TARGET_URL}/login")

        login_state = surface.perception.perceive()
        record(
            "web_surface: perceive() reads the login page",
            PASS if login_state.coverage.ok else FAIL,
            "" if login_state.coverage.ok else (login_state.coverage.reason or ""),
        )

        def find(predicate) -> object | None:
            return next((e for e in login_state.elements if predicate(e)), None)

        # Role has to be part of the match: the login page's label cell
        # ("Operator ID") and the textbox it names both carry that string, and
        # only the textbox has a handle to type into.
        username_field = find(lambda e: e.role == "textbox" and "operator id" in e.name.lower())
        password_field = find(lambda e: e.role == "textbox" and "passcode" in e.name.lower())
        submit_button = find(lambda e: e.role == "button" and "sign in" in e.name.lower())
        fields_found = bool(username_field and password_field and submit_button)
        record(
            "web_surface: login form fields are named and resolvable",
            PASS if fields_found else FAIL,
        )
        if not fields_found:
            return

        for label, action, ref, value in [
            ("type operator id", ActionKind.TYPE, username_field.ref, OPERATOR_USERNAME),
            ("type passcode", ActionKind.TYPE, password_field.ref, OPERATOR_PASSWORD),
            ("click sign in", ActionKind.CLICK, submit_button.ref, None),
        ]:
            result = do(action, ref, value, login_state)
            if not result.ok:
                record(f"web_surface: {label}", FAIL, result.error or "")
                return
        record("web_surface: fill + submit the login form", PASS)

        surface.settle.wait_until_settled(5.0)
        after_login = surface.perception.perceive()
        signed_in = "/search" in after_login.url
        record(
            "web_surface: login redirects to /search",
            PASS if signed_in else FAIL,
            after_login.url,
        )
        if not signed_in:
            return

        surface.goto(f"{TARGET_URL}/member/12345")
        member_state = surface.perception.perceive()
        balance_element = next(
            (e for e in member_state.elements if "4,812.55" in (e.value or "")), None
        )
        if balance_element is None:
            record(
                "web_surface: reads a known member's balance from the seeded fixture",
                FAIL,
                "balance text not found in perceived elements",
            )
            return

        read_result = do(ActionKind.READ, balance_element.ref, None, member_state)
        read_value = read_result.read_value or ""
        ok = bool(read_result.ok and "4,812.55" in read_value)
        record(
            "web_surface: READ action returns the seeded balance",
            PASS if ok else FAIL,
            "" if ok else f"read_value={read_result.read_value!r}",
        )
    except Exception as error:
        record("web_surface: end-to-end login -> read flow", FAIL, str(error))
    finally:
        surface.stop()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--skip-static", action="store_true", help="skip ruff/pyright/pytest")
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    args = parser.parse_args()

    print("=== 1. static checks (ruff, pyright, pytest) ===")
    if args.skip_static:
        record("static checks", SKIP, "--skip-static passed")
    else:
        check_static()

    print("\n=== 2. config ===")
    check_config()

    print("\n=== 3. target_app (boots a real server on :5000, or reuses one already running) ===")
    # A server already answering on :5000 is not necessarily one this script
    # started (e.g. left running from a manual test session) — reuse it and
    # say so plainly, rather than spawning a second uvicorn that fails to bind
    # the port while the health check keeps passing against the original one.
    already_running = _port_open(TARGET_HOST, TARGET_PORT)
    target_app_process: subprocess.Popen | None = None
    if already_running:
        record(
            "target_app: reusing a server already listening on :5000",
            SKIP,
            "not started or stopped by this script",
        )
    else:
        target_app_process = start_target_app()

    try:
        app_is_up = check_target_app_http(started_by_this_run=target_app_process is not None)

        print("\n=== 4. web_surface (real headless browser against target_app) ===")
        if app_is_up:
            check_web_surface(headed=args.headed)
        else:
            record("web_surface: end-to-end login -> read flow", SKIP, "target_app did not come up")
    finally:
        if target_app_process is not None:
            target_app_process.terminate()
            try:
                target_app_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                target_app_process.kill()

    print("\n=== summary ===")
    for name, status, _ in results:
        print(f"{status:5} {name}")

    failed = [r for r in results if r[1] == FAIL]
    print()
    if failed:
        print(f"{len(failed)} check(s) failed.")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
