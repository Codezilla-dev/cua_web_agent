"""Tests for the control gate at the surface, and for recording the human.

Both use fakes rather than a browser. The gate's contract is that it runs
*before* anything else in `execute`, which is exactly what a fake proves and a
real browser would only obscure; the recorder's contract is about what it does
with the events it receives, not about Chromium delivering them.
"""

from typing import Any, cast

import pytest

from src.policy.redact import Redactor
from src.session.lease import ControlDeniedError, LeaseOwner, LeaseStore
from src.surface.base import ControlGate
from src.surface.web import WebActions
from src.surface.web_handoff import WebHumanActionRecorder
from src.types import ActionKind, Expectation, Intent, RiskTier, TextVisibleCheck


class NoPage:
    """Stands in for the Playwright page in gate tests.

    The gate tests assert that `execute` refuses *before* it touches the page,
    so any attribute access here is a test failure rather than a fixture gap.
    """

    def __getattr__(self, name):
        raise AssertionError(f"the page was used despite the gate: .{name}")


class DenyingGate:
    def require_automation(self) -> None:
        raise ControlDeniedError("'operator-1' holds human control")


class AllowingGate:
    def require_automation(self) -> None:
        return None


def make_intent(action=ActionKind.CLICK, value=None):
    return Intent(
        reasoning="test",
        intent="test step",
        action=action,
        target_ref="e1",
        target_description="a button",
        value=value,
        risk=RiskTier.SAFE,
        expectation=Expectation(
            description="something happens",
            check=TextVisibleCheck(kind="text_visible", text="ok"),
        ),
    )


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #
def test_the_lease_store_satisfies_the_control_gate_protocol(tmp_path):
    # The surface deliberately does not import src.session; this is the check
    # that the two halves still fit despite never referring to each other.
    assert isinstance(LeaseStore(tmp_path / "lease.json"), ControlGate)


def test_automation_is_refused_at_the_surface_while_a_human_holds_control():
    actions = WebActions(page=cast(Any, NoPage()), action_timeout_ms=1000, control=DenyingGate())

    with pytest.raises(ControlDeniedError):
        actions.execute(make_intent(), target=None)


def test_the_gate_runs_before_argument_validation():
    # A CLICK with no target normally raises SurfaceError. Under a denying gate
    # it must raise ControlDeniedError instead: the action is not merely
    # malformed, it must not be attempted at all.
    actions = WebActions(page=cast(Any, NoPage()), action_timeout_ms=1000, control=DenyingGate())

    with pytest.raises(ControlDeniedError):
        actions.execute(make_intent(ActionKind.CLICK), target=None)


def test_a_run_with_no_escalation_wired_in_is_not_gated():
    actions = WebActions(page=cast(Any, NoPage()), action_timeout_ms=1000, control=None)

    # Falls through to the normal contract: CLICK without a target is misuse.
    with pytest.raises(Exception) as error:
        actions.execute(make_intent(ActionKind.CLICK), target=None)
    assert not isinstance(error.value, ControlDeniedError)


def test_a_real_lease_blocks_and_then_unblocks_the_surface(tmp_path):
    store = LeaseStore(tmp_path / "lease.json")
    actions = WebActions(page=cast(Any, NoPage()), action_timeout_ms=1000, control=store)

    store.acquire(LeaseOwner.HUMAN, "operator-1", reason="taking over")
    with pytest.raises(ControlDeniedError):
        actions.execute(make_intent(), target=None)

    store.acquire(LeaseOwner.AUTOMATION, "run", reason="resumed")
    # No longer ControlDeniedError -- back to the ordinary misuse error.
    with pytest.raises(Exception) as error:
        actions.execute(make_intent(), target=None)
    assert not isinstance(error.value, ControlDeniedError)


# --------------------------------------------------------------------------- #
# Recording the human
# --------------------------------------------------------------------------- #
class FakeFrame:
    def __init__(self, url):
        self.url = url
        self.evaluated: list[str] = []

    def evaluate(self, script):
        self.evaluated.append(script)


class FakePage:
    def __init__(self, url="http://localhost:5000/search"):
        self.url = url
        self.main_frame = FakeFrame(url)
        self.frames = [self.main_frame]
        self.exposed: dict = {}
        self.init_scripts: list[str] = []
        self.evaluated: list[str] = []
        self.handlers: dict = {}

    def expose_function(self, name, fn):
        self.exposed[name] = fn

    def add_init_script(self, script):
        self.init_scripts.append(script)

    def evaluate(self, script):
        self.evaluated.append(script)

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler):
        self.handlers.get(event, []).remove(handler)

    # -- helpers for the tests --
    def fire_dom(self, **payload):
        self.exposed["__cuaRecord"](payload)

    def navigate(self, url):
        self.url = url
        self.main_frame.url = url
        for handler in self.handlers.get("framenavigated", []):
            handler(self.main_frame)


def test_the_recorder_survives_the_operator_navigating():
    # A recorder installed only on the current document stops working the moment
    # the human clicks a link, which is most of what they came to do.
    page = FakePage()
    recorder = WebHumanActionRecorder(page)

    recorder.start()

    assert page.init_scripts, "no init script: capture would not survive navigation"
    assert page.main_frame.evaluated, "current document was never instrumented"


def test_clicks_and_inputs_are_captured_in_order():
    page = FakePage()
    recorder = WebHumanActionRecorder(page)
    recorder.start()

    page.fire_dom(kind="click", role="link", name="Open Record", value=None)
    page.fire_dom(kind="input", role="textbox", name="Member ID", value="22841")

    actions = recorder.stop()
    assert [a.kind for a in actions] == ["click", "input"]
    assert actions[0].name == "Open Record"
    assert actions[1].value == "22841"


def test_navigation_is_captured_and_not_repeated():
    page = FakePage()
    recorder = WebHumanActionRecorder(page)
    recorder.start()

    page.navigate("http://localhost:5000/member/22841")
    page.navigate("http://localhost:5000/member/22841")  # same URL fires twice

    actions = recorder.stop()
    assert [a.kind for a in actions] == ["navigate"]
    assert actions[0].url.endswith("/member/22841")


def test_sub_frame_navigation_is_ignored():
    # The target app renders results in an iframe; those fire constantly and are
    # noise in an audit log of what a person did.
    page = FakePage()
    recorder = WebHumanActionRecorder(page)
    recorder.start()

    for handler in page.handlers["framenavigated"]:
        handler(FakeFrame("http://localhost:5000/results-frame"))

    assert recorder.stop() == []


def test_a_secret_typed_by_the_operator_is_swept_from_the_record():
    # The page drops password fields at source, but an operator may type a
    # secret into a field this process cannot recognise as sensitive.
    page = FakePage()
    redactor = Redactor(
        sensitive_name_pattern=r"(?i)password", replacement="***", secret_values=["hunter2"]
    )
    recorder = WebHumanActionRecorder(page, redactor=redactor)
    recorder.start()

    page.fire_dom(kind="input", role="textbox", name="Notes", value="the code is hunter2")

    actions = recorder.stop()
    assert "hunter2" not in (actions[0].value or "")
    assert "***" in (actions[0].value or "")


def test_stopping_twice_is_harmless():
    page = FakePage()
    recorder = WebHumanActionRecorder(page)
    recorder.start()
    recorder.stop()

    assert recorder.stop() == []


def test_every_frame_is_instrumented_not_just_the_main_document():
    # The target app renders search results in an iframe, so the control an
    # operator is most likely to click during a drift incident lives in a child
    # frame. Instrumenting only the top document would miss it.
    page = FakePage()
    results_frame = FakeFrame("http://localhost:5000/results")
    page.frames.append(results_frame)
    recorder = WebHumanActionRecorder(page)

    recorder.start()

    assert results_frame.evaluated, "the iframe was never instrumented"


def test_one_unscriptable_frame_does_not_stop_the_others():
    class HostileFrame(FakeFrame):
        def evaluate(self, script):
            raise RuntimeError("cross-origin")

    page = FakePage()
    page.frames.append(HostileFrame("https://elsewhere.example/widget"))
    good = FakeFrame("http://localhost:5000/results")
    page.frames.append(good)
    recorder = WebHumanActionRecorder(page)

    recorder.start()

    assert good.evaluated, "a frame we could not script aborted the rest"
