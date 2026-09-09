"""Why a run stopped and needs a person.

"Max steps hit" is not a diagnosis. It is the symptom every different failure
shares, and routing all of them to a human with that one label makes the human
do the classification work the system should have done. So stuck is enumerated,
and each member is a *different thing for the operator to do*:

    LOCATOR_UNRESOLVED         the control is not there -- look at the page
    CHECKPOINT_FAILED_TERMINAL the flow ended somewhere unexpected -- read the state
    UNKNOWN_DIALOG             something is in the way -- dismiss or answer it
    POLICY_BLOCK_NEEDS_HUMAN   the action is risky by policy -- decide, don't debug
    NO_PROGRESS_N_STEPS        the page is not moving -- push it forward
    AUTH_EXPIRED               the session died -- sign back in

That mapping is the entire justification for the enum. If two members would
send the operator to do the same thing, they should be one member.

The last two are the ones the brief names that a step-level check would miss:
a session that expires mid-run and a run that makes no progress both look like
success at every individual step. Both were declared here long before anything
could raise them, which is the failure this enum is supposed to prevent -- a
member nothing produces sends nobody anywhere. Both now have detectors in
`replay/engine.py`:

**No progress** compares the page's text digest across each acting step and
counts how many in a row changed nothing. Reaching it needs a weak expectation
as well as a dead click, because a step whose assertion is specific enough fails
first and reports the more precise thing -- which is correct.

**Auth expired** looks for a password field reappearing *after* the flow has
already signed in. That is a narrow rule on purpose: a password box on the page
is unremarkable during login and means only one thing after it -- and "after"
has to be observed, not assumed. Having typed a credential is not having signed
in; the sign-in is the password field going away. Conflating the two armed the
detector during login and reported a run that could not find its renamed
"Log In" button as a dead session, which is precisely the mis-routing this enum
exists to prevent.
"""

from enum import StrEnum


class StuckCondition(StrEnum):
    """A named reason automation cannot safely continue."""

    # The step's target is not resolvable on the page in front of us.
    LOCATOR_UNRESOLVED = "locator_unresolved"
    # Every step ran but the flow did not arrive where the capability says.
    CHECKPOINT_FAILED_TERMINAL = "checkpoint_failed_terminal"
    # Something modal is in the way and no configured rule recognises it.
    UNKNOWN_DIALOG = "unknown_dialog"
    # Policy classified the action as needing a person, not as forbidden.
    POLICY_BLOCK_NEEDS_HUMAN = "policy_block_needs_human"
    # The page stopped changing across several acting steps: not progress.
    NO_PROGRESS_N_STEPS = "no_progress_n_steps"
    # A password field is on screen again after the flow had already signed in.
    AUTH_EXPIRED = "auth_expired"


# What the operator is being asked to do, per condition. Carried into the
# intervention request so the person reading it is not reverse-engineering the
# enum name.
OPERATOR_GUIDANCE: dict[StuckCondition, str] = {
    StuckCondition.LOCATOR_UNRESOLVED: (
        "The control this step needs was not found. Check whether the page is the one "
        "expected, and if the control is present under a different name, perform the step "
        "manually and resume."
    ),
    StuckCondition.CHECKPOINT_FAILED_TERMINAL: (
        "Every step ran but the flow did not end where the capability says it should. "
        "Confirm where the session actually is before resuming."
    ),
    StuckCondition.UNKNOWN_DIALOG: (
        "An unrecognised dialog is blocking the flow. Dismiss or answer it, then resume."
    ),
    StuckCondition.POLICY_BLOCK_NEEDS_HUMAN: (
        "Policy classified this action as one a person must authorise. This is a decision, "
        "not a malfunction: perform it yourself if it is correct, or abandon the run."
    ),
    StuckCondition.NO_PROGRESS_N_STEPS: (
        "The last few actions ran without changing the page at all -- the controls were "
        "found and used, and nothing happened. Check whether the page is waiting on "
        "something, then move the session forward manually or abandon the run."
    ),
    StuckCondition.AUTH_EXPIRED: (
        "The session appears to have been signed out: a password field is on screen "
        "again partway through the flow. Sign back in, return to where the flow was, "
        "and resume."
    ),
}
