"""Reading and writing artifacts, including the refusals.

`load` refusing an artifact it cannot vouch for is a stated acceptance
criterion, and it was the untested kind of claim -- the code was there, nothing
asserted it. The refusals matter more than the happy path: an artifact written
by a newer compiler may mean something different, and replay guessing at it is
worse than replay not running.
"""

import json
from datetime import UTC, datetime

import pytest

from src.artifact.models import (
    SCHEMA_VERSION,
    Capability,
    CapabilityStep,
    OutputSpec,
    ParamSpec,
    Provenance,
    SurfaceRef,
    TargetSpec,
)
from src.artifact.store import ArtifactError, artifact_path, dump, load
from src.types import ActionKind, Expectation, RiskTier, UrlContainsCheck

# Fixed, not `now()`. `recorded_at` comes from the trace in real compilation,
# which is what makes two compilations of the same trace byte-identical -- a
# fixture that moved would test the clock instead of the writer.
RECORDED_AT = datetime(2026, 9, 8, 23, 56, 18, tzinfo=UTC)


def a_capability(capability_id: str = "look_up_member_read") -> Capability:
    return Capability(
        id=capability_id,
        title="look up member {{member_id_or_name}} and read their savings balance",
        surface=SurfaceRef(
            kind="web", entry_path="/search", recorded_origin="http://localhost:5000"
        ),
        inputs=[ParamSpec(name="member_id_or_name", example="12345")],
        outputs=[OutputSpec(name="balance", description="balance", source_step=0)],
        steps=[
            CapabilityStep(
                index=0,
                intent="read the balance",
                action=ActionKind.READ,
                target=TargetSpec(
                    role="StaticText", name="Balance", scope="main", match_index=0
                ),
                risk=RiskTier.SAFE,
                expectation=Expectation(
                    description="on the record",
                    check=UrlContainsCheck(
                        kind="url_contains", fragment="{{member_id_or_name}}"
                    ),
                ),
            )
        ],
        checkpoint=Expectation(
            description="The URL identifies the requested record.",
            check=UrlContainsCheck(kind="url_contains", fragment="{{member_id_or_name}}"),
        ),
        provenance=Provenance(
            discovery_run_id="20260909T000000Z-test",
            goal="look up member 12345 and read their savings balance",
            model="test",
            recorded_at=RECORDED_AT,
            source_step_count=1,
            retried_step_count=0,
        ),
    )


# --- round trip -------------------------------------------------------------
def test_a_capability_survives_a_write_and_a_read_unchanged(tmp_path):
    original = a_capability()

    loaded = load(dump(original, tmp_path))

    assert loaded == original


def test_the_file_is_written_where_artifact_path_says_it_is(tmp_path):
    path = dump(a_capability("some_id"), tmp_path)

    assert path == artifact_path("some_id", tmp_path)


def test_writing_the_same_capability_twice_is_byte_identical(tmp_path):
    # Canonical JSON is what makes a diff in review mean something. Without it,
    # key reordering looks like a change to the capability.
    first = dump(a_capability(), tmp_path).read_bytes()
    second = dump(a_capability(), tmp_path).read_bytes()

    assert first == second


# --- refusals ---------------------------------------------------------------
def test_an_artifact_from_a_newer_compiler_is_refused(tmp_path):
    path = dump(a_capability(), tmp_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["schema_version"] = SCHEMA_VERSION + 1
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ArtifactError) as error:
        load(path)

    assert "schema_version" in str(error.value)
    assert str(SCHEMA_VERSION + 1) in str(error.value)


def test_an_artifact_with_no_schema_version_is_refused(tmp_path):
    path = dump(a_capability(), tmp_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    del raw["schema_version"]
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ArtifactError):
        load(path)


def test_malformed_json_is_refused_with_the_path_in_the_message(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text('{"schema_version": 1, "id": ', encoding="utf-8")

    with pytest.raises(ArtifactError) as error:
        load(path)

    assert "broken.json" in str(error.value)
    assert "not valid JSON" in str(error.value)


def test_a_missing_artifact_is_refused_rather_than_raising_an_os_error(tmp_path):
    with pytest.raises(ArtifactError) as error:
        load(tmp_path / "nothing_here.json")

    assert "no artifact at" in str(error.value)


def test_json_that_is_not_a_capability_is_refused(tmp_path):
    # Right version, wrong shape. Pydantic decides; the point is that `load`
    # does not hand a half-built capability back to replay.
    path = tmp_path / "wrong_shape.json"
    path.write_text(json.dumps({"schema_version": SCHEMA_VERSION}), encoding="utf-8")

    with pytest.raises(Exception) as error:
        load(path)

    assert not isinstance(error.value, KeyError)
