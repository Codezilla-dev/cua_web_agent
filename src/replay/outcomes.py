"""What a replay can tell its caller.

Three outcomes, not two. The distinction that matters most is between a *failure*
and a *business outcome*: "no member 99999 exists" is a correct, useful answer
that the caller needs to act on, not a crash. Collapsing the two is how an
automation ends up retrying a lookup that will never succeed, or paging a human
about a record that simply is not there.

    Success          the flow ran, the checkpoint held, here are the outputs
    BusinessOutcome  the application said something definitive and expected
    HardFailure      something broke; here is the step and what was wrong

`Recoverable` is deliberately absent from this union. Transient slowness and
known interstitials are handled inside the engine, bounded, and reported in the
result's `recoveries` — they are an implementation detail of getting to one of
the three outcomes, not an outcome the caller chooses between.

Business outcomes are matched from configuration rather than hardcoded, because
the phrase a system uses for "not found" is a property of that system, and a
second tenant running the same vendor app may word it differently.
"""

import re
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FailureKind(StrEnum):
    """Why a replay could not finish. Each is a different thing to go and fix."""

    # The step's target was not on the page. Either the flow moved or the app did.
    TARGET_NOT_FOUND = "target_not_found"
    # The target was found but the action failed against it.
    ACTION_FAILED = "action_failed"
    # The action ran, but the page did not become what the step expected.
    EXPECTATION_FAILED = "expectation_failed"
    # Every step ran, but the flow did not end where the capability says it should.
    CHECKPOINT_FAILED = "checkpoint_failed"
    # The guard stopped the step. Replay is not exempt from policy.
    POLICY_HALTED = "policy_halted"
    # A required parameter was missing or the artifact was unreadable.
    INVALID_REQUEST = "invalid_request"
    # The browser or the target went away.
    SURFACE_ERROR = "surface_error"


class Recovery(Base):
    """One thing the engine absorbed on the way to a result."""

    step_index: int
    condition: str
    action_taken: str
    attempts: int


class Success(Base):
    """The flow completed and the checkpoint held."""

    status: Literal["success"] = "success"
    outputs: dict[str, str] = Field(default_factory=dict)
    steps_run: int
    recoveries: list[Recovery] = Field(default_factory=list)


class BusinessOutcome(Base):
    """The application gave a definitive, expected answer that is not success.

    `code` is stable and machine-readable so a caller can branch on it;
    `detail` is what the screen actually said, for a human reading the log.
    """

    status: Literal["business_outcome"] = "business_outcome"
    code: str
    detail: str
    step_index: int
    recoveries: list[Recovery] = Field(default_factory=list)


class HardFailure(Base):
    """Replay could not continue, and it is not a normal application answer.

    Carries what a person needs to debug it without re-running: which step,
    what was expected, what was actually there, and where the evidence is.
    """

    status: Literal["hard_failure"] = "hard_failure"
    kind: FailureKind
    step_index: int
    step_intent: str = ""
    expected: str
    observed: str
    evidence_dir: str | None = None
    recoveries: list[Recovery] = Field(default_factory=list)
    # A more specific diagnosis than `kind`, when the engine has one: the
    # `StuckCondition` value this failure should escalate as. `kind` says what
    # went wrong at the step ("the expectation did not hold"); this says what is
    # actually going on ("the session has been signed out"), which is what
    # decides where the operator is sent.
    #
    # A string rather than the enum so that `outcomes` does not import
    # `escalation` -- the dependency runs the other way, and escalation is the
    # layer that knows what these mean.
    stuck_hint: str | None = None


ReplayResult = Success | BusinessOutcome | HardFailure


class BusinessOutcomeRule(Base):
    """A phrase that means the application has answered definitively."""

    code: str
    pattern: str

    def matches(self, text: str) -> bool:
        return re.search(self.pattern, text) is not None


def detect_business_outcome(
    rules: list[BusinessOutcomeRule], observed_text: str
) -> BusinessOutcomeRule | None:
    """First matching rule, or None. Order in config is the priority."""
    for rule in rules:
        if rule.matches(observed_text):
            return rule
    return None
