"""Typed configuration: `config/default.yaml` for policy, the environment for secrets."""

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict

from src.replay.outcomes import BusinessOutcomeRule, Field

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "default.yaml"


class FrozenModel(BaseModel):
    """Read-only once loaded."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class AllowlistSettings(FrozenModel):
    """What the agent may touch. Enforced in the guard stage."""

    origins: list[str]
    action_types: list[str]


class RiskPolicySettings(FrozenModel):
    """Inputs to the risk tier policy derives itself."""

    irreversible_name_pattern: str
    consequential_actions: list[str]
    safe_actions: list[str]


class RedactionSettings(FrozenModel):
    """Scrubbing applied before anything reaches disk."""

    sensitive_name_pattern: str
    replacement: str


class RunSettings(FrozenModel):
    """Loop budgets and per-step timing."""

    max_steps: int
    max_seconds: float
    settle_timeout_s: float


class PerceptionSettings(FrozenModel):
    """Thresholds for the coverage gate."""

    min_interactive_elements: int
    max_unnamed_ratio: float


class LlmSettings(FrozenModel):
    """LLM access, from the environment only.

    `api_key` and `model` are empty when nothing is configured, rather than
    `load_settings` refusing to build. Replay is the reason: it never consults a
    model -- a test asserts it cannot even import one -- so making it fail to
    start for want of an LLM key contradicts the property the whole production
    path is built on. Discovery raises where it actually needs the key, which is
    also where the error can say what to do about it.
    """

    provider: Literal["openai", "openrouter"]
    api_key: str = ""
    base_url: str
    model: str = ""
    # OpenRouter only; it retries these on the same request. Capped at 3 total.
    fallback_models: list[str] = Field(default_factory=list)

    @property
    def supports_model_routing(self) -> bool:
        """OpenRouter extension. OpenAI 400s on unrecognised body params."""
        return self.provider == "openrouter"


class TargetSettings(FrozenModel):
    """The surface under automation. Credentials never appear in a goal."""

    url: str
    username: str | None = None
    password: str | None = None


class ReplaySettings(FrozenModel):
    """Phrases that mean the application answered definitively.

    In config rather than in code because the wording belongs to the target app:
    two tenants running the same vendor product may say "not found" differently,
    and that should be a config change, not a release.
    """

    business_outcomes: list[BusinessOutcomeRule]


class Settings(FrozenModel):
    """Everything the run needs, in one object."""

    allowlist: AllowlistSettings
    risk_policy: RiskPolicySettings
    redaction: RedactionSettings
    run: RunSettings
    perception: PerceptionSettings
    replay: ReplaySettings
    llm: LlmSettings
    target: TargetSettings
    headed: bool = True

    def secret_values(self) -> list[str]:
        """Strings to scrub from traces wherever they appear."""
        candidates = [self.target.password, self.llm.api_key]
        return [value for value in candidates if value]


def _load_dotenv(path: Path) -> None:
    """`KEY=value` lines only. Real env vars win."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.split(" #", 1)[0].strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"{name} is not set. Copy .env.example to .env and fill it in."
        )
    return value


def _parse_model_list(raw: str | None) -> list[str]:
    """Comma-separated model ids, blanks dropped."""
    if not raw:
        return []
    return [model.strip() for model in raw.split(",") if model.strip()]


NO_LLM_CONFIGURED = (
    "No LLM key found. Set OPENAI_API_KEY (or OPENROUTER_API_KEY). "
    "Copy .env.example to .env and fill it in."
)


def require_llm(settings: "Settings") -> None:
    """Raise unless a model is actually configured.

    Called by the paths that need one. Kept separate from loading so that the
    paths that do not -- replay, the operator console -- start without a key.
    """
    if not settings.llm.api_key or not settings.llm.model:
        raise RuntimeError(NO_LLM_CONFIGURED)


def _load_llm_settings() -> LlmSettings:
    """Pick the provider from LLM_PROVIDER, else from whichever key is set.

    The model id is required, not defaulted: a stale default 404s mid-run. But
    "required" means required *of a run that uses a model*: with nothing
    configured this returns an unconfigured `LlmSettings` and lets the caller
    decide whether that matters. See `require_llm`.
    """
    provider = os.environ.get("LLM_PROVIDER", "").strip().lower()
    if not provider:
        if os.environ.get("OPENAI_API_KEY"):
            provider = "openai"
        elif os.environ.get("OPENROUTER_API_KEY"):
            provider = "openrouter"
        else:
            return LlmSettings(
                provider="openai", base_url="https://api.openai.com/v1"
            )
    if provider not in ("openai", "openrouter"):
        raise RuntimeError(
            f"LLM_PROVIDER={provider!r} is not supported. Use 'openai' or 'openrouter'."
        )

    if provider == "openai":
        return LlmSettings(
            provider="openai",
            api_key=_require_env("OPENAI_API_KEY"),
            base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            model=_require_env("OPENAI_MODEL"),
            fallback_models=[],
        )
    return LlmSettings(
        provider="openrouter",
        api_key=_require_env("OPENROUTER_API_KEY"),
        base_url=os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        model=_require_env("OPENROUTER_MODEL"),
        fallback_models=_parse_model_list(os.environ.get("OPENROUTER_FALLBACK_MODELS")),
    )


def load_settings(config_path: Path | None = None) -> Settings:
    """Read config + environment into a `Settings`."""
    _load_dotenv(REPO_ROOT / ".env")

    path = config_path or DEFAULT_CONFIG_PATH
    raw_config = yaml.safe_load(path.read_text(encoding="utf-8"))

    llm = _load_llm_settings()
    target = TargetSettings(
        url=os.environ.get("TARGET_URL", "https://parabank.parasoft.com/parabank/index.htm"),
        username=os.environ.get("TARGET_USERNAME") or None,
        password=os.environ.get("TARGET_PASSWORD") or None,
    )

    run_config = dict(raw_config["run"])
    # Shorten a run without editing tracked config.
    if os.environ.get("CUA_MAX_STEPS"):
        run_config["max_steps"] = int(os.environ["CUA_MAX_STEPS"])
    if os.environ.get("CUA_MAX_SECONDS"):
        run_config["max_seconds"] = float(os.environ["CUA_MAX_SECONDS"])

    return Settings(
        allowlist=AllowlistSettings(**raw_config["allowlist"]),
        risk_policy=RiskPolicySettings(**raw_config["risk_policy"]),
        redaction=RedactionSettings(**raw_config["redaction"]),
        run=RunSettings(**run_config),
        perception=PerceptionSettings(**raw_config["perception"]),
        replay=ReplaySettings(**raw_config["replay"]),
        llm=llm,
        target=target,
        headed=os.environ.get("CUA_HEADED", "1") == "1",
    )
