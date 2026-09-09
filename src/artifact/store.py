"""Reading and writing capability artifacts.

Canonical JSON with sorted keys and a trailing newline, so two compilations of
the same trace produce byte-identical files and a change shows up as a real diff
in review rather than as key reordering.
"""

import json
from pathlib import Path

from src.artifact.models import SCHEMA_VERSION, Capability

DEFAULT_ARTIFACT_DIR = Path("artifacts")


class ArtifactError(Exception):
    """An artifact could not be read, or is a version this build cannot run."""


def artifact_path(capability_id: str, root: str | Path = DEFAULT_ARTIFACT_DIR) -> Path:
    return Path(root) / f"{capability_id}.json"


def dump(capability: Capability, root: str | Path = DEFAULT_ARTIFACT_DIR) -> Path:
    """Write `capability` and return where it landed."""
    path = artifact_path(capability.id, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.loads(capability.model_dump_json())
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def load(path: str | Path) -> Capability:
    """Read a capability, refusing a schema version this build does not know.

    Refusing is the point: an artifact written by a newer compiler may mean
    something different, and replay guessing at it is worse than not running.
    """
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ArtifactError(f"no artifact at {path}") from error
    except json.JSONDecodeError as error:
        raise ArtifactError(f"{path} is not valid JSON: {error}") from error

    found = raw.get("schema_version")
    if found != SCHEMA_VERSION:
        raise ArtifactError(
            f"{path} has schema_version {found!r}, but this build reads {SCHEMA_VERSION}"
        )
    return Capability.model_validate(raw)
