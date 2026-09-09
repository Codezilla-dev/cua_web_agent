"""The verification scripts must log in with the fixture's credentials.

Layer 3 of the verification stack -- the three scripts that drive a real
browser against target_app -- was silently broken for a while: each held its
own copy of the passcode, the fixture's passcode changed, and all three began
failing at the login step. Nothing caught it, because nothing asserted that the
two agreed.

This is that assertion. It is deliberately about the *source*, not the runtime
behaviour: the scripts need a browser and an app to run, and a guard that only
fires under those conditions is a guard that does not fire.
"""

from pathlib import Path

import pytest

from target_app.data import OPERATOR_PASSWORD, OPERATOR_USERNAME

SCRIPTS = ["loop_dry_run.py", "smoke_test.py", "planner_live_check.py"]
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


@pytest.mark.parametrize("name", SCRIPTS)
def test_the_script_takes_its_credentials_from_the_fixture(name):
    source = (SCRIPTS_DIR / name).read_text(encoding="utf-8")

    assert "from target_app.data import" in source, (
        f"{name} must import the fixture's credentials rather than restating them"
    )
    assert "OPERATOR_USERNAME" in source and "OPERATOR_PASSWORD" in source


@pytest.mark.parametrize("name", SCRIPTS)
def test_the_script_does_not_hardcode_the_passcode(name):
    # The literal, not the name. A copy of the secret is what broke this before.
    source = (SCRIPTS_DIR / name).read_text(encoding="utf-8")

    assert OPERATOR_PASSWORD not in source, (
        f"{name} contains a literal copy of the fixture passcode"
    )


@pytest.mark.parametrize("name", SCRIPTS)
def test_the_script_does_not_hardcode_the_username(name):
    source = (SCRIPTS_DIR / name).read_text(encoding="utf-8")
    quoted = [f'"{OPERATOR_USERNAME}"', f"'{OPERATOR_USERNAME}'"]

    for literal in quoted:
        assert literal not in source, (
            f"{name} contains a literal copy of the fixture username ({literal})"
        )
