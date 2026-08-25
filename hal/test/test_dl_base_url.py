"""Tests for hal.config.dl_base_url() — gates autonomous's cloud DL services
(speaker-ID, pose, motion, SER, emotion, YOLO — the /hal/api/dl/... prefix)
behind an explicit dl_base_url or an actual autonomous.ai host, so a BYO LLM
host never gets a live request to a path it doesn't implement.
"""

import hal.config as hal_config


def test_explicit_config_key_wins_over_llm_base_url(monkeypatch):
    monkeypatch.setattr(
        hal_config, "_os_cfg_get",
        lambda key, default="": {
            "dl_base_url": "https://dl.example.com",
            "llm_base_url": "http://localhost:8080",
        }.get(key, default),
    )
    monkeypatch.delenv("DL_BACKEND_URL", raising=False)
    assert hal_config.dl_base_url() == "https://dl.example.com"


def test_explicit_env_var_wins_over_llm_base_url(monkeypatch):
    monkeypatch.setattr(
        hal_config, "_os_cfg_get",
        lambda key, default="": "http://localhost:8080" if key == "llm_base_url" else default,
    )
    monkeypatch.setenv("DL_BACKEND_URL", "https://dl.example.com")
    assert hal_config.dl_base_url() == "https://dl.example.com"


def test_llm_base_url_used_when_host_is_a_autonomous_ai_subdomain(monkeypatch):
    monkeypatch.setattr(
        hal_config, "_os_cfg_get",
        lambda key, default="": "https://campaign-api.autonomous.ai/api/v1/ai" if key == "llm_base_url" else default,
    )
    monkeypatch.delenv("DL_BACKEND_URL", raising=False)
    assert hal_config.dl_base_url() == "https://campaign-api.autonomous.ai/api/v1/ai"


def test_llm_base_url_on_bare_autonomous_domain_is_used(monkeypatch):
    monkeypatch.setattr(
        hal_config, "_os_cfg_get",
        lambda key, default="": "https://autonomous.ai" if key == "llm_base_url" else default,
    )
    monkeypatch.delenv("DL_BACKEND_URL", raising=False)
    assert hal_config.dl_base_url() == "https://autonomous.ai"


def test_llm_base_url_on_byo_host_returns_empty(monkeypatch):
    monkeypatch.setattr(
        hal_config, "_os_cfg_get",
        lambda key, default="": "http://192.168.1.50:8080/v1" if key == "llm_base_url" else default,
    )
    monkeypatch.delenv("DL_BACKEND_URL", raising=False)
    assert hal_config.dl_base_url() == ""


def test_lookalike_host_is_not_treated_as_autonomous(monkeypatch):
    """A hostname that merely starts with the real domain (but is actually a
    subdomain of something else) must not match — only an exact host or a
    genuine *.autonomous.ai subdomain does."""
    monkeypatch.setattr(
        hal_config, "_os_cfg_get",
        lambda key, default="": "https://autonomous.ai.evil.com" if key == "llm_base_url" else default,
    )
    monkeypatch.delenv("DL_BACKEND_URL", raising=False)
    assert hal_config.dl_base_url() == ""


def test_nothing_configured_returns_empty(monkeypatch):
    monkeypatch.setattr(hal_config, "_os_cfg_get", lambda key, default="": default)
    monkeypatch.delenv("DL_BACKEND_URL", raising=False)
    assert hal_config.dl_base_url() == ""
