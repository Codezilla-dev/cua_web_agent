"""Provider selection for LLM access.

`_load_llm_settings` is the one place that decides which endpoint a run talks
to, and it branches on environment variables that a reviewer will set by hand.
It is pure (environment in, frozen model out, no I/O), so it is tested the same
way the coverage check, verifier, policy and redaction are.

The property that matters most here is `supports_model_routing`: sending
OpenRouter's `models` array to OpenAI is a 400 on every call, so the wrong
answer breaks a run outright rather than degrading.
"""

from __future__ import annotations

import pytest

from src.config import _load_llm_settings, load_settings, require_llm

LLM_ENV_PREFIXES = ("OPENAI_", "OPENROUTER_", "LLM_")


@pytest.fixture(autouse=True)
def _clear_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test from an environment with no LLM variables set.

    Without this, a developer's real `.env`-exported shell would leak into the
    test and make the result depend on the machine it ran on.
    """
    import os

    for name in list(os.environ):
        if name.startswith(LLM_ENV_PREFIXES):
            monkeypatch.delenv(name, raising=False)


def test_openai_key_alone_selects_openai_and_disables_model_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-test")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-example")

    settings = _load_llm_settings()

    assert settings.provider == "openai"
    assert settings.base_url == "https://api.openai.com/v1"
    assert settings.model == "gpt-example"
    # The load-bearing assertion: OpenAI rejects the `models` array with a 400.
    assert settings.supports_model_routing is False
    assert settings.fallback_models == []


def test_openrouter_key_alone_selects_openrouter_and_enables_model_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-test")
    monkeypatch.setenv("OPENROUTER_MODEL", "vendor/primary:free")
    monkeypatch.setenv("OPENROUTER_FALLBACK_MODELS", "vendor/a:free, vendor/b:free")

    settings = _load_llm_settings()

    assert settings.provider == "openrouter"
    assert settings.base_url == "https://openrouter.ai/api/v1"
    assert settings.supports_model_routing is True
    assert settings.fallback_models == ["vendor/a:free", "vendor/b:free"]


def test_llm_provider_breaks_the_tie_when_both_keys_are_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both keys set is ambiguous, so the explicit variable must win.

    Inferring from key presence alone would silently pick whichever branch
    happens to be checked first, which is exactly the kind of implicit
    behaviour that makes a run hard to explain afterwards.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-test")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-example")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-test")
    monkeypatch.setenv("OPENROUTER_MODEL", "vendor/primary:free")
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")

    assert _load_llm_settings().provider == "openrouter"

    monkeypatch.setenv("LLM_PROVIDER", "openai")
    assert _load_llm_settings().provider == "openai"


def test_a_custom_base_url_overrides_the_provider_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Azure/proxy deployments keep the OpenAI request shape at another host."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-test")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-example")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://proxy.internal/v1")

    assert _load_llm_settings().base_url == "https://proxy.internal/v1"


def test_no_key_at_all_yields_an_unconfigured_llm_rather_than_raising() -> None:
    # Loading used to raise here, which meant `cua replay` could not start on a
    # machine with no API key -- for a code path that is asserted, by another
    # test, to be incapable of importing a model client. The refusal moved to
    # `require_llm`, which discovery calls and replay does not.
    settings = _load_llm_settings()

    assert settings.api_key == ""
    assert settings.model == ""


def test_a_missing_model_fails_at_startup_rather_than_mid_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model id is required precisely because a stale default 404s mid-run.

    A defaulted slug goes out of date silently -- the provider retires it and
    the next run dies several steps in, with an HTTP error that says nothing
    about configuration. Failing before the browser opens is far cheaper.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-test")

    with pytest.raises(RuntimeError, match="OPENAI_MODEL is not set"):
        _load_llm_settings()


def test_an_unsupported_provider_is_rejected_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")

    with pytest.raises(RuntimeError, match="not supported"):
        _load_llm_settings()


# --- replay must not need a model -------------------------------------------
def test_settings_load_with_no_llm_configured(monkeypatch, tmp_path):
    # Replay never consults a model -- a test asserts it cannot even import one
    # -- so `load_settings` refusing to build without an LLM key contradicted
    # the property the whole production path is built on. Replaying a recorded
    # capability on a machine with no API key is the normal production case.
    for name in (
        "LLM_PROVIDER", "OPENAI_API_KEY", "OPENAI_MODEL",
        "OPENROUTER_API_KEY", "OPENROUTER_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("src.config._load_dotenv", lambda *a, **k: None)

    settings = load_settings()

    assert settings.llm.api_key == ""
    assert settings.llm.model == ""


def test_require_llm_raises_only_where_a_model_is_actually_needed(monkeypatch):
    for name in (
        "LLM_PROVIDER", "OPENAI_API_KEY", "OPENAI_MODEL",
        "OPENROUTER_API_KEY", "OPENROUTER_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("src.config._load_dotenv", lambda *a, **k: None)
    settings = load_settings()

    with pytest.raises(RuntimeError) as error:
        require_llm(settings)

    assert "OPENAI_API_KEY" in str(error.value)


def test_require_llm_is_satisfied_by_a_configured_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4.1")
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setattr("src.config._load_dotenv", lambda *a, **k: None)

    require_llm(load_settings())  # must not raise
