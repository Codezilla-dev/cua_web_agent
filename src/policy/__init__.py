"""The guard stage: `check.py` for allow/halt, `redact.py` for trace scrubbing.

Both are pure code over already-produced objects.
"""

from src.policy.check import Policy
from src.policy.redact import Redactor

__all__ = ["Policy", "Redactor"]
