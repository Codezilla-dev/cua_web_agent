"""The guard stage, between decide and act. Never shown to the model.

Four checks, first miss wins: origin allowlist, action allowlist, risk tier
derived independently of `Intent.risk`, then halt on `IRREVERSIBLE`.

`target_url` is unused so far -- the allowlist is static config, not derived
from where a run started. Kept for a future "never leave this site" rule.
"""

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.config import Settings
from urllib.parse import urlsplit

from src.policy._targets import resolve_target_name
from src.types import ActionKind, Intent, PolicyVerdict, RiskTier, UIState

# (tier, human-readable reason)
TierOutcome = tuple[RiskTier, str]

# Only click/select can commit something. Typing into a field named
# "Confirm Password" is still reversible; the commit is a later click.
ACTIONS_CHECKED_FOR_IRREVERSIBLE_NAME = frozenset({ActionKind.CLICK, ActionKind.SELECT})


class Policy:
    """Allow/halt decisions from explicit config values."""

    def __init__(
        self,
        allowed_origins: list[str],
        allowed_action_types: list[str],
        irreversible_name_pattern: str,
        consequential_actions: list[str],
        safe_actions: list[str],
    ) -> None:
        self._allowed_origins = set(allowed_origins)
        self._allowed_action_types = set(allowed_action_types)
        self._irreversible_pattern = re.compile(irreversible_name_pattern)
        self._consequential_actions = set(consequential_actions)
        self._safe_actions = set(safe_actions)

    @classmethod
    def from_settings(cls, settings: "Settings") -> "Policy":
        """Build the guard from configuration.

        Both the discovery loop and replay are subject to the same policy -- a
        recorded capability is not a licence to skip the allowlist -- so the
        wiring lives here rather than being repeated at each call site where the
        two could drift apart.
        """
        return cls(
            allowed_origins=settings.allowlist.origins,
            allowed_action_types=settings.allowlist.action_types,
            irreversible_name_pattern=settings.risk_policy.irreversible_name_pattern,
            consequential_actions=settings.risk_policy.consequential_actions,
            safe_actions=settings.risk_policy.safe_actions,
        )

    def check(self, intent: Intent, state: UIState, target_url: str) -> PolicyVerdict:
        # Derived unconditionally so every verdict carries a real tier.
        tier, tier_reason = self._derive_tier(intent, state)

        origin_violation = self._origin_violation(intent, state)
        if origin_violation is not None:
            return PolicyVerdict(
                allowed=False,
                tier=tier,
                rule="origin_allowlist",
                reason=origin_violation,
                disposition="halt",
            )

        if intent.action not in self._allowed_action_types:
            return PolicyVerdict(
                allowed=False,
                tier=tier,
                rule="action_allowlist",
                reason=(
                    f"action {intent.action.value!r} is not in the allowed action types "
                    f"{sorted(self._allowed_action_types)}"
                ),
                disposition="halt",
            )

        if tier is RiskTier.IRREVERSIBLE:
            return PolicyVerdict(
                allowed=False,
                tier=tier,
                rule="risk.irreversible",
                reason=tier_reason,
                disposition="halt",
            )

        return PolicyVerdict(
            allowed=True,
            tier=tier,
            rule="allow",
            reason=f"passed origin, action, and risk checks ({tier_reason})",
            disposition="allow",
        )

    # -- individual checks --------------------------------------------------- #

    def _origin_violation(self, intent: Intent, state: UIState) -> str | None:
        """`None` if both origin checks pass, else a human-readable reason."""
        current_origin = _origin(state.url)
        if current_origin not in self._allowed_origins:
            return (
                f"current page origin {current_origin!r} is not in the allowed "
                f"origins {sorted(self._allowed_origins)}"
            )

        # Only NAVIGATE has a destination to pre-check.
        if intent.action is ActionKind.NAVIGATE and intent.value:
            destination_origin = _origin(intent.value)
            if destination_origin not in self._allowed_origins:
                return (
                    f"navigate target {intent.value!r} has origin {destination_origin!r}, "
                    f"which is not in the allowed origins {sorted(self._allowed_origins)}"
                )

        return None

    def _derive_tier(self, intent: Intent, state: UIState) -> TierOutcome:
        """Policy's own risk tier for `intent`, independent of `Intent.risk`."""
        if intent.action in ACTIONS_CHECKED_FOR_IRREVERSIBLE_NAME:
            target_name = resolve_target_name(intent.target_ref, state.elements)
            if target_name and self._irreversible_pattern.search(target_name):
                return (
                    RiskTier.IRREVERSIBLE,
                    f"target {target_name!r} matches the irreversible-action name pattern",
                )

        action_value = intent.action.value
        if action_value in self._consequential_actions:
            return RiskTier.CONSEQUENTIAL, f"action {action_value!r} is configured as consequential"
        if action_value in self._safe_actions:
            return RiskTier.SAFE, f"action {action_value!r} is configured as safe"

        # Unclassified action = a config gap. Fail closed.
        return (
            RiskTier.IRREVERSIBLE,
            f"action {action_value!r} is not classified as safe or consequential in "
            "risk_policy config; treated as irreversible until configured",
        )


def _origin(url: str) -> str:
    """`scheme://host[:port]`. An unparseable URL yields one nothing matches."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"
