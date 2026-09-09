"""A `Surface` made of scripted pages, so replay can be tested without a browser.

`ReplayRequest` takes the surface as an argument, which is the whole reason this
is possible: the engine never constructs one. So the 600 lines that decide
whether a run is a success, a business outcome or a failure can be driven from a
list of `UIState`s and asserted on directly, in milliseconds, with no target app
and no Playwright.

The fake is deliberately dumb. It replays a scripted sequence of pages, one per
`perceive()`, and records every action it was asked to perform. It does not
simulate a browser -- a fake that tried to would be a second implementation of
the thing under test, and its bugs would look like the engine's.
"""

from datetime import UTC, datetime

from src.policy.check import Policy
from src.surface.base import (
    ActionExecutor,
    PerceptionProvider,
    SettleDetector,
    SurfaceError,
    TargetNotFoundError,
)
from src.types import (
    ActionKind,
    ActResult,
    Intent,
    PerceptionCoverage,
    ResolvedTarget,
    SettleResult,
    UIElement,
    UIState,
)

ALL_ACTIONS = ["navigate", "click", "type", "select", "read", "wait", "done"]


def permissive_policy(
    allowed_action_types: list[str] | None = None,
    irreversible_name_pattern: str = r"(?i)confirm|transfer|delete",
) -> Policy:
    """The real `Policy`, configured to allow the tests' fixture flow.

    A stand-in that returned `allow` would prove replay calls *something*.
    Driving the real guard proves replay is subject to the policy the rest of
    the system uses, which is the claim worth testing -- so the halt cases
    below narrow this configuration rather than substituting a fake verdict.
    """
    return Policy(
        allowed_origins=["http://localhost:5000"],
        allowed_action_types=allowed_action_types or ALL_ACTIONS,
        irreversible_name_pattern=irreversible_name_pattern,
        consequential_actions=["click", "select"],
        safe_actions=["navigate", "type", "read", "wait", "done"],
    )


def element(
    ref: str,
    role: str = "button",
    name: str = "",
    value: str | None = None,
    scope: str = "main",
) -> UIElement:
    return UIElement(ref=ref, role=role, name=name, value=value, scope=scope)


def page(
    url: str,
    elements: list[UIElement] | None = None,
    title: str = "page",
    text_digest: str | None = None,
) -> UIState:
    """One scripted page. `text_digest` doubles as the page's visible text.

    The engine reads `text_digest` when matching business-outcome rules and
    `elements` when matching targets and checking expectations, so a scripted
    page only needs the two.
    """
    elements = elements or []
    return UIState(
        url=url,
        title=title,
        elements=elements,
        text_digest=text_digest if text_digest is not None else " ".join(
            e.name for e in elements
        ),
        coverage=PerceptionCoverage(
            ok=True, reason=None, interactive_count=len(elements), unnamed_ratio=0.0
        ),
        captured_at=datetime.now(UTC),
    )


class FakePerception:
    def __init__(self, surface: "FakeSurface") -> None:
        self._surface = surface

    def perceive(self, screenshot_path: str | None = None) -> UIState:
        return self._surface.next_page()

    def resolve(self, target_ref: str, state: UIState) -> ResolvedTarget:
        for candidate in state.elements:
            if candidate.ref == target_ref:
                return ResolvedTarget(ref=target_ref, element=candidate, resolved_by="ref")
        raise TargetNotFoundError(f"ref {target_ref!r} is not in this snapshot")


class FakeActions:
    """Performs nothing and records everything.

    `read` returns whatever the scripted element's `value` holds, which is how a
    test says what the page shows without a DOM. A step listed in
    `surface.failing_actions` reports failure instead, so the ACTION_FAILED path
    is reachable.
    """

    def __init__(self, surface: "FakeSurface") -> None:
        self._surface = surface

    def execute(self, intent: Intent, target: ResolvedTarget | None) -> ActResult:
        self._surface.performed.append(intent)
        if intent.action in self._surface.failing_actions:
            return ActResult(
                ok=False,
                action=intent.action,
                duration_ms=1,
                error=self._surface.failing_actions[intent.action],
            )
        read_value = None
        if intent.action is ActionKind.READ:
            read_value = (target.element.value if target else None) or ""
        return ActResult(ok=True, action=intent.action, duration_ms=1, read_value=read_value)


class FakeSettle:
    def __init__(self, surface: "FakeSurface") -> None:
        self._surface = surface

    def wait_until_settled(self, timeout_seconds: float) -> SettleResult:
        self._surface.settles += 1
        return SettleResult(settled=True, waited_ms=0, signal="dom_stable")


class FakeSurface:
    """A surface that hands out `pages` in order, one per `perceive()`.

    The last page repeats once the script runs out. That is deliberate: a test
    cares about the page a particular step sees, and padding the tail with
    copies to satisfy an exact call count would make every test depend on how
    many times the engine happens to perceive per step.
    """

    def __init__(
        self,
        pages: list[UIState],
        failing_actions: dict[ActionKind, str] | None = None,
        fail_on_start: str | None = None,
    ) -> None:
        assert pages, "a fake surface needs at least one page"
        self._pages = pages
        self._index = 0
        self._fail_on_start = fail_on_start
        self.failing_actions = failing_actions or {}

        self.performed: list[Intent] = []
        self.perceived: list[UIState] = []
        self.settles = 0
        self.started = False
        self.stopped = False
        self.visited: list[str] = []

        # Annotated with the protocol types, not the concrete ones. `Surface`
        # declares these as mutable attributes, which makes them invariant, so
        # a fake that says `FakePerception` here does not satisfy the protocol
        # even though the class itself does.
        self.perception: PerceptionProvider = FakePerception(self)
        self.actions: ActionExecutor = FakeActions(self)
        self.settle: SettleDetector = FakeSettle(self)

    @property
    def settle_timeout_s(self) -> float:
        return 0.0

    def next_page(self) -> UIState:
        state = self._pages[min(self._index, len(self._pages) - 1)]
        self._index += 1
        self.perceived.append(state)
        return state

    def start(self) -> None:
        if self._fail_on_start is not None:
            raise SurfaceError(self._fail_on_start)
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def goto(self, url: str) -> None:
        self.visited.append(url)
