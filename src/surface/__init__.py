"""Surface port + platform implementations.

`base.py` defines the protocols. `web.py` (Playwright) and `desktop.py` (stub)
implement them. Import platform libraries only inside this package.
"""

from src.surface.base import (
    ActionExecutor,
    PerceptionProvider,
    SettleDetector,
    Surface,
    SurfaceError,
    TargetNotFoundError,
)

__all__ = [
    "ActionExecutor",
    "PerceptionProvider",
    "SettleDetector",
    "Surface",
    "SurfaceError",
    "TargetNotFoundError",
]
