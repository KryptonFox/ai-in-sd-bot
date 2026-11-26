import tarfile
from pathlib import Path

import pytest
from fastmcp.client.transports import StdioTransport

from app.config import ConfigError, Settings
from app.mcp.connection import create_student_tools_client
from mcp_servers.student_tools.config import (
    DEFAULT_EXA_WEB_SEARCH_ENDPOINT,
    DEFAULT_EXA_WEB_SEARCH_TIMEOUT_SECONDS,
    DEFAULT_WEB_SEARCH_ENDPOINT,
    ServerSettings,
    WebSearchProviderName,
)
from scripts.cloud_ssh import _deployment_environment, source_archive

TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk"
SECRET = "s" * 32


def server_settings(tmp_path: Path, **environment: str) -> ServerSettings:
    values = {
        "POSTGRES_PASSWORD": "unused",
        "MCP_SHARED_SECRET": SECRET,
        **environment,
    }
    config = tmp_path / "config.toml"
    config.write_text("", encoding="utf-8")
    return ServerSettings.load(
        tmp_path / "missing.env",
        environ=values,
        config_file=config,
    )


def app_settings(**environment: str) -> Settings:
    values = {
        "BOT_TOKEN": TOKEN,
        "POSTGRES_PASSWORD": "unused",
        "MCP_SHARED_SECRET": SECRET,
        **environment,
    }
    return Settings.load(Path("/nonexistent/missing.env"), environ=values)


def test_student_settings_default_to_disabled_web_search(tmp_path: Path) -> None:
    settings = server_settings(tmp_path)

    assert settings.web_search_provider is WebSearchProviderName.DISABLED
    assert settings.web_search_api_key == ""
    assert settings.web_search_endpoint == DEFAULT_WEB_SEARCH_ENDPOINT
    assert settings.web_search_max_results == 5
    assert "web_search_api_key" not in repr(settings)


def test_student_settings_load_brave_search_configuration(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        "\n".join(
            [
                "[web_search]",
                'provider = "brave"',
                'endpoint = "https://search.example.test/v1/web"',
                "timeout_seconds = 5.5",
                "max_results = 7",
            ]
        ),
        encoding="utf-8",
    )
    settings = ServerSettings.load(
        tmp_path / "missing.env",
        environ={
            "POSTGRES_PASSWORD": "unused",
            "MCP_SHARED_SECRET": SECRET,
            "WEB_SEARCH_API_KEY": "brave-private-key",
        },
        config_file=config,
    )

    assert settings.web_search_provider is WebSearchProviderName.BRAVE
    assert settings.web_search_api_key == "brave-private-key"
    assert settings.web_search_endpoint == "https://search.example.test/v1/web"
    assert settings.web_search_timeout_seconds == 5.5
    assert settings.web_search_max_results == 7
    assert "brave-private-key" not in repr(settings)


def test_student_settings_load_keyless_exa_search_configuration(tmp_path: Path) -> None:
    settings = server_settings(tmp_path, WEB_SEARCH_PROVIDER="exa")

    assert settings.web_search_provider is WebSearchProviderName.EXA
    assert settings.web_search_api_key == ""
    assert settings.web_search_endpoint == DEFAULT_EXA_WEB_SEARCH_ENDPOINT
    assert settings.web_search_timeout_seconds == DEFAULT_EXA_WEB_SEARCH_TIMEOUT_SECONDS


def test_exa_provider_replaces_default_brave_endpoint(tmp_path: Path) -> None:
    settings = server_settings(
        tmp_path,
        WEB_SEARCH_PROVIDER="exa",
        WEB_SEARCH_ENDPOINT=DEFAULT_WEB_SEARCH_ENDPOINT,
        WEB_SEARCH_TIMEOUT_SECONDS="12",
    )

    assert settings.web_search_endpoint == DEFAULT_EXA_WEB_SEARCH_ENDPOINT
    assert settings.web_search_timeout_seconds == 12.0


def test_exa_endpoint_is_completed_with_tools_parameter(tmp_path: Path) -> None:
    settings = server_settings(
        tmp_path,
        WEB_SEARCH_PROVIDER="exa",
        WEB_SEARCH_ENDPOINT="https://mcp.exa.ai/mcp",
    )

    assert settings.web_search_endpoint == DEFAULT_EXA_WEB_SEARCH_ENDPOINT


def test_exa_endpoint_from_toml_allows_tools_query(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        "\n".join(
            [
                "[web_search]",
                'provider = "exa"',
                'endpoint = "https://mcp.exa.ai/mcp?tools=web_search_exa"',
                "max_results = 4",
            ]
        ),
        encoding="utf-8",
    )
    settings = ServerSettings.load(
        tmp_path / "missing.env",
        environ={"POSTGRES_PASSWORD": "unused", "MCP_SHARED_SECRET": SECRET},
        config_file=config,
    )

    assert settings.web_search_provider is WebSearchProviderName.EXA
    assert settings.web_search_endpoint == DEFAULT_EXA_WEB_SEARCH_ENDPOINT
    assert settings.web_search_max_results == 4


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://mcp.exa.ai/mcp?tools=web_search_exa",
        "https://mcp.exa.ai/mcp?exaApiKey=secret",
        "https://mcp.exa.ai/mcp?tools=web_fetch_exa",
        "https://user:password@mcp.exa.ai/mcp?tools=web_search_exa",
    ],
)
def test_student_settings_reject_unsafe_exa_endpoint(tmp_path: Path, endpoint: str) -> None:
    with pytest.raises(ConfigError, match="WEB_SEARCH_ENDPOINT"):
        server_settings(
            tmp_path,
            WEB_SEARCH_PROVIDER="exa",
            WEB_SEARCH_ENDPOINT=endpoint,
        )


@pytest.mark.parametrize(
    ("environment", "message"),
    [
        ({"WEB_SEARCH_PROVIDER": "unknown"}, "WEB_SEARCH_PROVIDER"),
        ({"WEB_SEARCH_PROVIDER": "brave"}, "WEB_SEARCH_API_KEY"),
        (
            {
                "WEB_SEARCH_PROVIDER": "brave",
                "WEB_SEARCH_API_KEY": "key",
                "WEB_SEARCH_ENDPOINT": "http://search.example.test/v1/web",
            },
            "WEB_SEARCH_ENDPOINT",
        ),
        (
            {
                "WEB_SEARCH_PROVIDER": "brave",
                "WEB_SEARCH_API_KEY": "key",
                "WEB_SEARCH_TIMEOUT_SECONDS": "0",
            },
            "WEB_SEARCH_TIMEOUT_SECONDS",
        ),
        (
            {
                "WEB_SEARCH_PROVIDER": "brave",
                "WEB_SEARCH_API_KEY": "key",
                "WEB_SEARCH_MAX_RESULTS": "9",
            },
            "WEB_SEARCH_MAX_RESULTS",
        ),
    ],
)
def test_student_settings_reject_invalid_web_search_configuration(
    tmp_path: Path,
    environment: dict[str, str],
    message: str,
) -> None:
    with pytest.raises(ConfigError, match=message):
        server_settings(tmp_path, **environment)


def test_application_settings_carry_web_search_environment_without_repr_leak() -> None:
    settings = app_settings(
        WEB_SEARCH_PROVIDER="brave",
        WEB_SEARCH_API_KEY="configured-secret",
        WEB_SEARCH_MAX_RESULTS="3",
        GOOGLE_API_KEY="google-secret",
    )

    assert settings.web_search_environment == {
        "WEB_SEARCH_PROVIDER": "brave",
        "WEB_SEARCH_API_KEY": "configured-secret",
        "WEB_SEARCH_MAX_RESULTS": "3",
    }
    assert "configured-secret" not in repr(settings)
    assert "GOOGLE_API_KEY" not in settings.web_search_environment


def test_stdio_student_environment_uses_configured_web_search_values_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ROUTER_API_KEY", "router-secret")
    monkeypatch.setenv("GOOGLE_API_KEY", "google-secret")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-secret")
    monkeypatch.setenv("WEB_SEARCH_API_KEY", "ambient-secret")
    settings = Settings(
        bot_token="unused",
        postgres_password="database-password",
        mcp_shared_secret="stdio-shared-secret",
        web_search_environment={
            "WEB_SEARCH_PROVIDER": "brave",
            "WEB_SEARCH_API_KEY": "configured-secret",
            "WEB_SEARCH_MAX_RESULTS": "3",
            "UNRELATED": "must-not-pass",
        },
    )

    client = create_student_tools_client(settings)
    transport = client._client.transport

    assert isinstance(transport, StdioTransport)
    assert transport.env is not None
    assert transport.env["WEB_SEARCH_PROVIDER"] == "brave"
    assert transport.env["WEB_SEARCH_API_KEY"] == "configured-secret"
    assert transport.env["WEB_SEARCH_MAX_RESULTS"] == "3"
    assert transport.env["MCP_SHARED_SECRET"] == "stdio-shared-secret"
    assert "ROUTER_API_KEY" not in transport.env
    assert "GOOGLE_API_KEY" not in transport.env
    assert "GEMINI_API_KEY" not in transport.env
    assert "UNRELATED" not in transport.env
    assert "ambient-secret" not in transport.env.values()


def test_opencode_go_fallback_key_requires_https() -> None:
    insecure = Settings(
        bot_token="unused",
        postgres_password="unused",
        llm_api_key="llm-secret",
        llm_base_url="http://opencode.ai/inference/go/openai/v1",
        router_base_url="https://opencode.ai/inference/go/openai/v1",
    )
    secure = Settings(
        bot_token="unused",
        postgres_password="unused",
        llm_api_key="llm-secret",
        llm_base_url="https://opencode.ai/inference/go/openai/v1",
        router_base_url="https://opencode.ai/inference/go/openai/v1",
    )

    assert insecure.effective_router_api_key == ""
    assert secure.effective_router_api_key == "llm-secret"


def test_deployment_environment_and_archive_include_web_search_and_materials(
    tmp_path: Path,
) -> None:
    settings = Settings(
        bot_token=TOKEN,
        postgres_password="unused",
        mcp_shared_secret=SECRET,
        web_search_environment={
            "WEB_SEARCH_PROVIDER": "brave",
            "WEB_SEARCH_API_KEY": "deployed-secret",
            "WEB_SEARCH_MAX_RESULTS": "4",
            "UNRELATED": "must-not-deploy",
        },
    )
    values = _deployment_environment(settings)

    assert values["WEB_SEARCH_PROVIDER"] == "brave"
    assert values["WEB_SEARCH_API_KEY"] == "deployed-secret"
    assert values["WEB_SEARCH_MAX_RESULTS"] == "4"
    assert "UNRELATED" not in values

    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("", encoding="utf-8")
    materials = tmp_path / "mcp_servers" / "student_tools" / "data" / "materials"
    materials.mkdir(parents=True)
    (materials / "approved.md").write_text("# Approved", encoding="utf-8")
    (tmp_path / "mcp_servers" / "student_tools" / "__init__.py").write_text(
        "",
        encoding="utf-8",
    )

    archive_path = source_archive(tmp_path)
    with tarfile.open(archive_path) as archive:
        names = set(archive.getnames())

    assert "mcp_servers/student_tools/data/materials/approved.md" in names
