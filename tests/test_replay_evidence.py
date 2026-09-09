"""Tests for the replay evidence bundle.

A replay leaves the same kind of trail a discovery run does, for the same
reason: a result you cannot audit is a result you have to take on trust. But a
replay step is not a discovery step -- there is no planner `Decision` behind it
-- so it gets its own record type rather than a `StepRecord` with a fabricated
decision in it.

The load-bearing test here is the last one: credentials must not reach disk.
Replay binds a real password into a real action, which is exactly the condition
under which the discovery loop leaked one.
"""

import json

from src.policy.redact import Redactor
from src.replay.evidence import ReplayEvidenceWriter, ReplayStepRecord
from src.replay.outcomes import BusinessOutcome, FailureKind, HardFailure, Success
from src.types import ActionKind

REPLACEMENT = "***REDACTED***"


def make_redactor(secrets=("hunter2",)) -> Redactor:
    return Redactor(
        sensitive_name_pattern=r"(?i)password|passcode",
        replacement=REPLACEMENT,
        secret_values=secrets,
    )


def make_record(index=0, **overrides) -> ReplayStepRecord:
    fields = {
        "index": index,
        "intent": "type the member id",
        "action": ActionKind.TYPE,
        "target_role": "textbox",
        "target_name": "Member ID or Name",
        "target_scope": "main",
        "target_match_index": 0,
        "value_kind": "param",
        "duration_ms": 12,
    }
    fields.update(overrides)
    return ReplayStepRecord(**fields)


def read_lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_each_step_is_one_line_in_trace_jsonl(tmp_path):
    writer = ReplayEvidenceWriter("run-1", make_redactor(), root=tmp_path)

    writer.append_step(make_record(0))
    writer.append_step(make_record(1, intent="click search"))

    lines = read_lines(writer.evidence_dir / "trace.jsonl")
    assert [line["index"] for line in lines] == [0, 1]
    assert lines[1]["intent"] == "click search"


def test_result_json_records_a_success_with_its_outputs(tmp_path):
    writer = ReplayEvidenceWriter("run-2", make_redactor(), root=tmp_path)

    writer.write_result(
        Success(outputs={"savings_account_balance": "$4,812.55"}, steps_run=7, recoveries=[]),
        capability_id="look_up_member_read",
        params={"member_id_or_name": "12345"},
    )

    result = json.loads((writer.evidence_dir / "result.json").read_text(encoding="utf-8"))
    assert result["outcome"] == "success"
    assert result["capability_id"] == "look_up_member_read"
    assert result["params"] == {"member_id_or_name": "12345"}
    assert result["outputs"] == {"savings_account_balance": "$4,812.55"}


def test_result_json_records_a_business_outcome_as_an_answer_not_an_error(tmp_path):
    writer = ReplayEvidenceWriter("run-3", make_redactor(), root=tmp_path)

    writer.write_result(
        BusinessOutcome(code="RECORD_NOT_FOUND", detail="No member records match", step_index=4),
        capability_id="look_up_member_read",
        params={"member_id_or_name": "99999"},
    )

    result = json.loads((writer.evidence_dir / "result.json").read_text(encoding="utf-8"))
    assert result["outcome"] == "business_outcome"
    assert result["code"] == "RECORD_NOT_FOUND"
    assert result["step_index"] == 4


def test_result_json_records_a_hard_failure_with_expected_and_observed(tmp_path):
    writer = ReplayEvidenceWriter("run-4", make_redactor(), root=tmp_path)

    writer.write_result(
        HardFailure(
            kind=FailureKind.TARGET_NOT_FOUND,
            step_index=2,
            expected="button named 'Search'",
            observed="a login page",
        ),
        capability_id="look_up_member_read",
        params={},
    )

    result = json.loads((writer.evidence_dir / "result.json").read_text(encoding="utf-8"))
    assert result["outcome"] == "hard_failure"
    assert result["kind"] == "target_not_found"
    assert result["expected"] == "button named 'Search'"
    assert result["observed"] == "a login page"


def test_a_credential_never_reaches_the_trace(tmp_path):
    # The discovery loop leaked a password through seven fields that were not
    # `intent.value`. Replay writes fewer fields, but the same rule applies to
    # all of them, so this asserts on the file rather than on any one field.
    writer = ReplayEvidenceWriter("run-5", make_redactor(secrets=("hunter2",)), root=tmp_path)

    writer.append_step(
        make_record(
            0,
            intent="type the password hunter2 into the Passcode field",
            target_name="Passcode",
            value_kind="credential",
            observed="Passcode contains hunter2",
            check_performed="field_has_value(Passcode) == 'hunter2'",
        )
    )

    written = (writer.evidence_dir / "trace.jsonl").read_text(encoding="utf-8")
    assert "hunter2" not in written
    assert REPLACEMENT in written


def test_a_credential_never_reaches_the_result(tmp_path):
    writer = ReplayEvidenceWriter("run-6", make_redactor(secrets=("hunter2",)), root=tmp_path)

    writer.write_result(
        HardFailure(
            kind=FailureKind.EXPECTATION_FAILED,
            step_index=1,
            expected="the Passcode field to contain hunter2",
            observed="hunter2 was rejected",
        ),
        capability_id="cap",
        params={"password_echo": "hunter2"},
    )

    written = (writer.evidence_dir / "result.json").read_text(encoding="utf-8")
    assert "hunter2" not in written


def test_step_png_path_is_zero_padded_under_the_run_directory(tmp_path):
    writer = ReplayEvidenceWriter("run-7", make_redactor(), root=tmp_path)

    path = writer.step_png_path(3)

    assert path.endswith("step_03.png")
    assert str(writer.evidence_dir) in path


# --- declared personal data -------------------------------------------------
#
# Credentials and personal data are different problems. A credential is the same
# string on every run and the run was told what it is, so it can be swept by
# value. A member id is different on every invocation and is only sensitive
# because the capability's author declared it so -- `ParamSpec.sensitivity`.
def test_a_pii_parameter_is_masked_everywhere_it_reached_the_bundle(tmp_path):
    # Not just in `params`. The same identifier arrives through the typed value,
    # the bound expectation and the observed URL, which is exactly the mistake
    # the credential sweep already made once.
    writer = ReplayEvidenceWriter(
        run_id="run-pii",
        redactor=Redactor(sensitive_name_pattern="(?i)password", replacement="***REDACTED***"),
        root=tmp_path,
        masked_values=["22841"],
    )

    writer.append_step(
        ReplayStepRecord(
            index=0,
            intent="type the member id 22841",
            action=ActionKind.TYPE,
            target_name="Member ID or Name",
            check_performed="field 'Member ID or Name' holds '22841'",
            observed="URL was 'http://localhost:5000/member/22841'",
        )
    )

    written = (tmp_path / "run-pii" / "trace.jsonl").read_text(encoding="utf-8")
    assert "22841" not in written
    assert written.count("***PII***") == 3


def test_a_public_parameter_is_left_readable(tmp_path):
    # The default. A trace with every identifier masked is the state the evidence
    # is least useful in, so masking is opt-in per parameter.
    writer = ReplayEvidenceWriter(
        run_id="run-public",
        redactor=Redactor(sensitive_name_pattern="(?i)password", replacement="***REDACTED***"),
        root=tmp_path,
    )

    writer.append_step(
        ReplayStepRecord(
            index=0, intent="type the member id 22841", action=ActionKind.TYPE
        )
    )

    written = (tmp_path / "run-public" / "trace.jsonl").read_text(encoding="utf-8")
    assert "22841" in written


def test_a_pii_parameter_is_masked_in_the_result_as_well_as_the_trace(tmp_path):
    writer = ReplayEvidenceWriter(
        run_id="run-pii-result",
        redactor=Redactor(sensitive_name_pattern="(?i)password", replacement="***REDACTED***"),
        root=tmp_path,
        masked_values=["22841"],
    )

    writer.write_result(
        Success(outputs={"savings_account_balance": "$918.40"}, steps_run=7),
        capability_id="look_up_member_read",
        params={"member_id_or_name": "22841"},
    )

    written = (tmp_path / "run-pii-result" / "result.json").read_text(encoding="utf-8")
    assert "22841" not in written
    assert "***PII***" in written
    # The run is still auditable: what it did and what it returned survive.
    assert "$918.40" in written


def test_masking_pii_does_not_stop_credentials_being_scrubbed(tmp_path):
    # Two passes, two markers, both applied.
    writer = ReplayEvidenceWriter(
        run_id="run-both",
        redactor=Redactor(
            sensitive_name_pattern="(?i)password",
            replacement="***REDACTED***",
            secret_values=["vault-echo-77-quill"],
        ),
        root=tmp_path,
        masked_values=["22841"],
    )

    writer.append_step(
        ReplayStepRecord(
            index=0,
            intent="member 22841 with passcode vault-echo-77-quill",
            action=ActionKind.TYPE,
        )
    )

    written = (tmp_path / "run-both" / "trace.jsonl").read_text(encoding="utf-8")
    assert "22841" not in written
    assert "vault-echo-77-quill" not in written
    assert "***PII***" in written and "***REDACTED***" in written
