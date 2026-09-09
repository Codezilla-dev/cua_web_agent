"""Replay must not be able to consult a model.

The whole value of the production path is that it is deterministic. That is an
architectural claim, and architectural claims rot quietly -- one convenient
import during a debugging session and replay is quietly asking a model again,
with nothing failing to say so. This test is the thing that says so.

**It follows the whole import graph, not just the first hop.** Scanning only
`src/replay/*.py` leaves the invariant one indirection from being false: replay
imports `src.agent.verify` today, and the day someone adds `import httpx` to
that module, replay can reach a model client while this test stays green. So
every `src.*` module reachable from `src/replay/` is walked transitively, and
the offending chain is printed rather than just the offending module -- "replay
is not model-free" is not actionable; "engine -> verify -> httpx" is.
"""

import ast
from collections import deque
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
REPLAY_PACKAGE = SRC / "replay"

# Modules that would put a model back in the decision loop.
FORBIDDEN_MODULES = ("src.agent.planner", "src.agent.loop", "httpx", "openai", "anthropic")


def imported_modules(path: Path) -> set[str]:
    """Every module named by an import in `path`."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def module_path(module: str, src_root: Path = SRC) -> Path | None:
    """The file backing a `src.*` module name, if this repo has one.

    Returns None for third-party modules and for `from src.x import Thing`
    where `Thing` is a name inside `src/x.py` rather than a module of its own --
    the package `__init__` is checked in that case instead.

    `src_root` is a parameter rather than a module global so the guard can be
    pointed at a synthetic tree and shown to fail; patching the global is
    unreliable when pytest imports this file under two names.
    """
    if not module.startswith("src"):
        return None
    relative = Path(*module.split(".")[1:])
    candidates = (
        src_root / relative.with_suffix(".py"),
        src_root / relative / "__init__.py",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    # `from src.artifact.models import Capability` names a class, not a module.
    parent = src_root / relative.parent
    if parent.is_dir() and (parent / "__init__.py").is_file():
        return parent / "__init__.py"
    return None


def is_forbidden(module: str) -> bool:
    return any(module == bad or module.startswith(f"{bad}.") for bad in FORBIDDEN_MODULES)


def reachable_offences(
    replay_package: Path = REPLAY_PACKAGE, src_root: Path = SRC
) -> list[str]:
    """Every forbidden import reachable from `replay_package`, as an import chain."""
    offences: list[str] = []
    seen: set[Path] = set()
    queue: deque[tuple[Path, list[str]]] = deque(
        (path, [path.stem]) for path in sorted(replay_package.glob("*.py"))
    )

    while queue:
        path, chain = queue.popleft()
        if path in seen:
            continue
        seen.add(path)

        for module in sorted(imported_modules(path)):
            if is_forbidden(module):
                offences.append(" -> ".join([*chain, module]))
                continue
            following = module_path(module, src_root)
            if following is not None and following not in seen:
                queue.append((following, [*chain, module]))
    return offences


def test_replay_never_imports_a_model_client():
    offences = reachable_offences()

    assert not offences, (
        "replay must stay model-free, but these chains reach a model client: "
        + "; ".join(offences)
        + ". If a model is genuinely needed here, that is an architectural change, "
        "not an import."
    )


def test_the_walk_actually_leaves_the_replay_package():
    # A closure test that never followed an edge would pass for the wrong
    # reason. Replay imports src.agent.verify, so the walk must reach it.
    reached: set[Path] = set()
    queue: deque[Path] = deque(sorted(REPLAY_PACKAGE.glob("*.py")))
    while queue:
        path = queue.popleft()
        if path in reached:
            continue
        reached.add(path)
        for module in imported_modules(path):
            following = module_path(module)
            if following is not None:
                queue.append(following)

    assert (SRC / "agent" / "verify.py") in reached
    assert len(reached) > len(list(REPLAY_PACKAGE.glob("*.py")))


def test_the_guard_catches_a_violation_one_indirection_away(tmp_path):
    # The failure the direct-imports-only version could not see: replay imports
    # a module that is clean today, and that module later imports httpx.
    package = tmp_path / "src"
    (package / "replay").mkdir(parents=True)
    (package / "agent").mkdir(parents=True)
    (package / "replay" / "engine.py").write_text(
        "from src.agent.verify import verify_expectation\n", encoding="utf-8"
    )
    (package / "agent" / "verify.py").write_text("import httpx\n", encoding="utf-8")

    offences = reachable_offences(package / "replay", package)

    assert offences == ["engine -> src.agent.verify -> httpx"]


def test_the_guard_would_actually_catch_a_direct_violation(tmp_path):
    # A test that can never fail is not a guard, so this proves the detector
    # actually sees a forbidden import rather than merely finding none.
    offender = tmp_path / "offender.py"
    offender.write_text(
        "import httpx\nfrom src.agent.planner import Planner\n", encoding="utf-8"
    )

    modules = imported_modules(offender)

    assert "httpx" in modules
    assert "src.agent.planner" in modules
