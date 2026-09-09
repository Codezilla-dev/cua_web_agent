"""Turning a label into a weaker, still-meaningful assertion.

Dropping an overfitted expectation is not enough. A step with no expectation is
a step replay cannot verify, and the whole reason the discovery loop forces the
planner to declare an expectation before acting is so that no step is taken on
faith. So a `record_data` expectation is *demoted*, not deleted: replaced with
the strongest claim that is still true for every input.

**An expectation is checked after the action, which decides the whole ladder.**
The first version of this got that wrong: it demoted a step to "the control this
step used is visible", which is a claim about the page *before* the click. Step 5
of the real capability clicks a link called "Open Record", and after that click
the session is on the member page where no such link exists -- so the demoted
expectation failed for every input, including the one it was recorded with. The
generalisation was worse than the overfitting it replaced. Found by replaying it.

So the replacement depends on what the action does to the page:

1. **An action that navigates** (`click`, `navigate`) is followed by a *different*
   page, and the structural fact about that page is its URL. Where the capability
   has a parameter, the fragment is templated -- `{{member_id_or_name}}` -- so the
   assertion is about the record that was *requested*, which is exactly the
   distinction overfitting loses. That is a stronger claim than it looks: it fails
   if the flow lands on the wrong record, not merely on some page.
2. **An action that does not navigate** (`type`, `select`) leaves the control in
   place, so `element_visible` on the step's own target is both true and useful.
3. **Nothing better available.** The expectation is left alone and reported as
   un-generalised. Silently weakening an assertion to something vacuous is worse
   than admitting the compiler could not improve it.

A `read` step is never demoted here. Replay already treats a read specially: its
recorded expectation quotes the value it read, which is circular, so the engine
asserts that the read returned *something* instead. Rewriting it would be a
second mechanism for a problem already solved.
"""

from collections.abc import Mapping
from urllib.parse import urlparse

from src.artifact.compiler import CREDENTIAL_PLACEHOLDER
from src.artifact.models import Capability, CapabilityStep
from src.artifact.semantics import Distillation
from src.types import ActionKind, ElementVisibleCheck, Expectation, UrlContainsCheck


def apply(
    capability: Capability,
    distillation: Distillation,
    step_urls: Mapping[int, str] | None = None,
    final_url: str = "",
) -> tuple[Capability, list[str]]:
    """Return a generalised copy of `capability`, plus a line per change.

    The notes are returned rather than logged so the caller decides where they
    go; the CLI prints them, because a compilation that quietly rewrote four
    assertions is one a reviewer should be told about.

    `step_urls` and `final_url` come from the recording, via
    `compiler.recorded_urls`, and they are what stops this pass asserting a URL
    shape that never occurred. Both are optional because every failure in this
    module has to be a no-op: without them the URL-templating rung of the
    ladder is skipped rather than guessed at.
    """
    step_urls = step_urls or {}
    overfitted = distillation.record_data_steps()
    notes: list[str] = []
    steps: list[CapabilityStep] = []

    for step in capability.steps:
        # `READ` steps are handled upstream, deterministically. A read whose
        # expectation quotes the value the read itself returned is decidable by
        # comparing two recorded strings, so `compiler._unquote_read_expectation`
        # settles it with no model involved -- and settles it even when this
        # pass never runs. Skipping them here used to mean nothing looked at
        # them at all, which left a capability asserting a specific bank balance
        # on every replay.
        if step.index not in overfitted or step.action is ActionKind.READ:
            steps.append(step)
            continue
        if _already_input_following(step.expectation):
            # The model called this record-fitted; the artifact says otherwise,
            # and the artifact is checkable. A check whose expected value is a
            # parameter template or a credential placeholder already follows the
            # caller's input -- it cannot be fitted to the recorded record,
            # because it does not contain a recorded value at all.
            #
            # This guard exists because the semantic pass is not deterministic:
            # across two runs on the same artifact the same model labelled these
            # steps "structural" once and "record_data" once. Where a rule can
            # settle it, the rule wins.
            notes.append(
                f"step {step.index}: labelled record_data, but its expectation is already "
                f"input-following -- kept"
            )
            steps.append(step)
            continue
        demoted = _demote(step, capability, step_urls.get(step.index, ""))
        if demoted is None:
            notes.append(
                f"step {step.index}: expectation quotes recorded data, but no structural "
                f"claim was available -- left unchanged"
            )
            steps.append(step)
            continue
        notes.append(
            f"step {step.index}: expectation demoted to {demoted.check.kind} "
            f"(was {step.expectation.check.kind} quoting recorded data)"
        )
        steps.append(step.model_copy(update={"expectation": demoted}))

    checkpoint = capability.checkpoint
    if distillation.checkpoint_is_record_specific and _already_input_following(
        capability.checkpoint
    ):
        # The same rule-beats-judgement call as the per-step guard above. The
        # compiler now refuses to emit a parameterised capability whose
        # checkpoint names no parameter, so a checkpoint that arrives here with
        # a `{{param}}` in it follows the caller's input by construction --
        # whatever the semantic pass labelled it.
        notes.append(
            "checkpoint: labelled record-specific, but it already follows the caller's "
            "input -- kept"
        )
    elif distillation.checkpoint_is_record_specific:
        replacement = _demote_checkpoint(capability, final_url)
        if replacement is not None:
            notes.append(
                f"checkpoint demoted to {replacement.check.kind}: the recorded one asserted "
                "data belonging to the recorded record"
            )
            checkpoint = replacement
        else:
            notes.append(
                "checkpoint quotes recorded data, but no structural claim was available -- "
                "left unchanged"
            )

    return capability.model_copy(update={"steps": steps, "checkpoint": checkpoint}), notes


def _already_input_following(expectation: Expectation) -> bool:
    """True when the expectation asserts a value the *caller* supplies.

    `{{member_id_or_name}}` is bound per invocation and `<credential>` is
    resolved from configuration, so neither can quote the recorded record.

    Every literal the check carries is examined rather than just `expected`:
    a checkpoint the compiler templated holds its placeholder in `fragment`,
    and reading only one attribute would miss it.
    """
    check = expectation.check
    for attribute in ("expected", "fragment", "text", "name_contains"):
        literal = getattr(check, attribute, None)
        if isinstance(literal, str) and (
            "{{" in literal or literal == CREDENTIAL_PLACEHOLDER
        ):
            return True
    return False


# Actions after which the page is expected to be a different one. An
# expectation is asserted *after* the action, so for these the step's own
# control is exactly the wrong thing to look for.
NAVIGATING = {ActionKind.CLICK, ActionKind.NAVIGATE}


def _demote(step: CapabilityStep, capability: Capability, url: str) -> Expectation | None:
    if step.action not in NAVIGATING and step.target is not None:
        return Expectation(
            description=(
                f"The {step.target.role} named {step.target.name!r} is still present "
                "after this step."
            ),
            check=ElementVisibleCheck(
                kind="element_visible",
                role=step.target.role,
                name_contains=step.target.name,
            ),
        )

    templated = _templated_fragment(capability, url)
    if templated:
        return Expectation(
            description=f"The URL identifies the requested record ({templated}).",
            check=UrlContainsCheck(kind="url_contains", fragment=templated),
        )
    fragment = _entry_path(capability)
    if fragment:
        return Expectation(
            description=f"The URL still contains {fragment!r}.",
            check=UrlContainsCheck(kind="url_contains", fragment=fragment),
        )
    return None


def _templated_fragment(capability: Capability, url: str) -> str:
    """`{{param}}`, so the assertion follows the caller's input rather than the
    recorded one -- but only for a parameter that demonstrably identifies this
    page.

    The check against `url` is the whole point of the helper. It used to return
    the first input that had an example at all, which is right for the step
    that lands on `/member/12345` and wrong for every other navigating step:
    the click that submits the login form lands on `/search`, and asserting
    that `/search` contains the member id is a check that fails on every
    invocation, including the recorded one. Templating a value into a URL claim
    is only honest when that value was in the URL.
    """
    if not url:
        return ""
    ordered = sorted(
        capability.inputs, key=lambda spec: len(spec.example or ""), reverse=True
    )
    for spec in ordered:
        if spec.example and spec.example in url:
            return f"{{{{{spec.name}}}}}"
    return ""


def _demote_checkpoint(capability: Capability, final_url: str) -> Expectation | None:
    """A checkpoint has to prove arrival, so the URL shape is the honest claim.

    The recorded run ended on a record page whose path contains the input --
    `/member/12345`. Templating the parameter back in gives a checkpoint that is
    specific to *the record that was asked for* rather than to the one that
    happened to be recorded, which is exactly the distinction that was missing.
    """
    templated = _templated_fragment(capability, final_url)
    if templated:
        return Expectation(
            description=f"The URL identifies the requested record ({templated}).",
            check=UrlContainsCheck(kind="url_contains", fragment=templated),
        )
    return None


def _entry_path(capability: Capability) -> str:
    path = urlparse(capability.surface.entry_path or "").path or capability.surface.entry_path
    return path.strip() if path and path != "/" else ""
