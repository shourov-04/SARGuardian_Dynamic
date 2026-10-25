from __future__ import annotations

from app import config


def test_local_environment_uses_backend_dotenv_without_overriding_host_values(monkeypatch) -> None:
    dotenv_path = "test-fixtures/Backend/.env"
    calls: list[dict[str, object]] = []

    def fake_load_dotenv(**kwargs: object) -> bool:
        calls.append(kwargs)
        return True

    monkeypatch.setattr(config, "load_dotenv", fake_load_dotenv)
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("VERCEL", raising=False)

    assert config.load_local_environment(dotenv_path) is True
    assert calls == [{"dotenv_path": config.Path(dotenv_path), "override": False}]


def test_production_skips_local_dotenv_loading(monkeypatch) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(config, "load_dotenv", lambda **kwargs: calls.append(kwargs) or True)
    monkeypatch.setenv("APP_ENV", "production")

    assert config.load_local_environment("test-fixtures/Backend/.env") is False
    assert calls == []


def test_vercel_skips_local_dotenv_loading(monkeypatch) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(config, "load_dotenv", lambda **kwargs: calls.append(kwargs) or True)
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.setenv("VERCEL", "1")

    assert config.load_local_environment("test-fixtures/Backend/.env") is False
    assert calls == []


def test_runtime_settings_centralize_non_secret_defaults_and_bounds() -> None:
    settings = config.get_settings({
        "APP_ENV": "staging",
        "CORS_ORIGINS": "https://staging.example.test",
        "SUPABASE_URL": "https://staging-project.supabase.co",
        "SUPABASE_SERVICE_ROLE_KEY": "sb_secret_test_only",
        "ASF_MAX_RESULTS": "10",
        "ASF_TIMEOUT_SECONDS": "15",
        "PREFLIGHT_TTL_SECONDS": "3600",
        "PORT": "8080",
    })
    assert settings.app_env == "staging"
    assert settings.asf_max_results == 10
    assert settings.asf_timeout_seconds == 15
    assert settings.preflight_ttl_seconds == 3600
    assert settings.port == 8080


def test_production_settings_require_server_supabase_configuration() -> None:
    settings = config.get_settings({
        "APP_ENV": "production",
        "CORS_ORIGINS": "https://staging.example.test",
    })
    try:
        config.validate_production_settings(settings)
    except config.SettingsError as error:
        assert str(error) == "SUPABASE_URL must be configured in production."
    else:
        raise AssertionError("production validation unexpectedly accepted missing Supabase settings")


def test_enabled_displacement_actions_requires_server_only_settings() -> None:
    settings = config.get_settings({
        "APP_ENV": "production", "CORS_ORIGINS": "https://app.example.test",
        "SUPABASE_URL": "https://project.supabase.co", "SUPABASE_SERVICE_ROLE_KEY": "sb_secret_test_only",
        "DISPLACEMENT_ACTIONS_ENABLED": "true",
    })
    try:
        config.validate_production_settings(settings)
    except config.SettingsError as error:
        assert "GITHUB_ACTIONS_TOKEN" in str(error)
    else:
        raise AssertionError("enabled Actions integration unexpectedly accepted missing settings")


def test_external_mode_keeps_callback_as_default_and_does_not_require_callback_secrets() -> None:
    assert config.get_settings({}).displacement_workflow_mode == "callback"
    settings = config.get_settings({
        "APP_ENV": "production", "CORS_ORIGINS": "https://app.example.test",
        "SUPABASE_URL": "https://project.supabase.co", "SUPABASE_SERVICE_ROLE_KEY": "sb_secret_test_only",
        "DISPLACEMENT_ACTIONS_ENABLED": "true", "DISPLACEMENT_WORKFLOW_MODE": "external",
        "GITHUB_ACTIONS_TOKEN": "github_pat_test_only", "GITHUB_ACTIONS_REPOSITORY": "owner/repository",
        "GITHUB_ACTIONS_WORKFLOW": "external.yml", "GITHUB_ACTIONS_REF": "main",
    })
    assert config.validate_production_settings(settings) == settings


def test_invalid_external_mode_or_repository_is_rejected_safely() -> None:
    try:
        config.get_settings({"DISPLACEMENT_WORKFLOW_MODE": "unexpected"})
    except config.SettingsError as error:
        assert str(error) == "DISPLACEMENT_WORKFLOW_MODE is invalid."
    else:
        raise AssertionError("invalid workflow mode was accepted")

    settings = config.get_settings({
        "APP_ENV": "production", "CORS_ORIGINS": "https://app.example.test",
        "SUPABASE_URL": "https://project.supabase.co", "SUPABASE_SERVICE_ROLE_KEY": "sb_secret_test_only",
        "DISPLACEMENT_ACTIONS_ENABLED": "true", "DISPLACEMENT_WORKFLOW_MODE": "external",
        "GITHUB_ACTIONS_TOKEN": "github_pat_test_only", "GITHUB_ACTIONS_REPOSITORY": "invalid",
        "GITHUB_ACTIONS_WORKFLOW": "external.yml", "GITHUB_ACTIONS_REF": "main",
    })
    try:
        config.validate_production_settings(settings)
    except config.SettingsError as error:
        assert str(error) == "GITHUB_ACTIONS_REPOSITORY must look like owner/repository."
    else:
        raise AssertionError("invalid external repository was accepted")
