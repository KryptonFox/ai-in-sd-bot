from pathlib import Path

import pytest

from app.config import ConfigError, Settings, public_url
from mcp_servers.math_tools.config import MathConfigError, MathSettings

TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk"
ROOT = Path(__file__).resolve().parents[1]


def settings_from(
    tmp_path: Path,
    *,
    config_text: str = "",
    env_text: str = "",
    environ: dict[str, str] | None = None,
) -> Settings:
    config_file = tmp_path / "config.toml"
    config_file.write_text(config_text, encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text(env_text, encoding="utf-8")
    base_environment = {
        "BOT_TOKEN": TOKEN,
        "POSTGRES_PASSWORD": "unused",
    }
    base_environment.update(environ or {})

    return Settings.load(env_file, environ=base_environment, config_file=config_file)


def math_settings_from(
    tmp_path: Path,
    *,
    config_text: str = "",
    env_text: str = "",
    environ: dict[str, str] | None = None,
) -> MathSettings:
    config_file = tmp_path / "config.toml"
    config_file.write_text(config_text, encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text(env_text, encoding="utf-8")

    return MathSettings.load(env_file, environ=environ or {}, config_file=config_file)


def test_toml_values_override_code_defaults(tmp_path: Path) -> None:
    settings = settings_from(
        tmp_path,
        config_text="""
[llm]
model = "toml-model"
max_tokens = 2048
generation_reserve_tokens = 1024

[history]
max_messages = 21

[mcp]
math_request_timeout_seconds = 45.0
""",
    )

    assert settings.llm_model == "toml-model"
    assert settings.llm_max_tokens == 2048
    assert settings.llm_generation_reserve_tokens == 1024
    assert settings.history_max_messages == 21
    assert settings.math_mcp_request_timeout_seconds == 45.0


def test_dotenv_overrides_toml_and_process_environment_overrides_dotenv(tmp_path: Path) -> None:
    config_text = """
[llm]
model = "toml-model"
generation_reserve_tokens = 1024

[agent]
max_tool_calls = 3
"""
    env_text = "LLM_MODEL=dotenv-model\nAGENT_MAX_TOOL_CALLS=4\n"

    from_dotenv = settings_from(tmp_path, config_text=config_text, env_text=env_text)
    from_environment = settings_from(
        tmp_path,
        config_text=config_text,
        env_text=env_text,
        environ={"LLM_MODEL": "process-model", "AGENT_MAX_TOOL_CALLS": "6"},
    )
    with_empty_process = settings_from(
        tmp_path,
        config_text=config_text,
        env_text=env_text,
        environ={"LLM_MODEL": "", "AGENT_MAX_TOOL_CALLS": ""},
    )

    assert from_dotenv.llm_model == "dotenv-model"
    assert from_dotenv.agent_max_tool_calls == 4
    assert from_environment.llm_model == "process-model"
    assert from_environment.agent_max_tool_calls == 6
    assert from_environment.llm_generation_reserve_tokens == 1024
    assert with_empty_process.llm_model == "dotenv-model"
    assert with_empty_process.agent_max_tool_calls == 4


def test_generation_reserve_is_capped_by_actual_max_output(tmp_path: Path) -> None:
    settings = settings_from(
        tmp_path,
        config_text="""
[llm]
max_tokens = 1024
generation_reserve_tokens = 4096
""",
    )

    assert settings.llm_generation_reserve_tokens == 4096
    assert settings.effective_generation_reserve_tokens == 1024


def test_unknown_section_and_parameter_are_rejected_with_location(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"config\.toml: \[service\]: неизвестная секция"):
        settings_from(tmp_path, config_text='[service]\nlog_level = "DEBUG"\n')

    with pytest.raises(
        ConfigError,
        match=r"config\.toml: \[llm\]\.temperature: неизвестный параметр",
    ):
        settings_from(tmp_path, config_text="[llm]\ntemperature = 0.2\n")


@pytest.mark.parametrize(
    ("config_text", "expected"),
    [
        ('[llm]\nmax_tokens = "many"\n', r"\[llm\]\.max_tokens: значение должно быть целым"),
        ("[llm]\nstreaming = 1\n", r"\[llm\]\.streaming: укажите true или false"),
        ('[mcp]\ntransport = "sse"\n', r"\[mcp\]\.transport: допустимые значения"),
        (
            "[llm]\ntimeout_seconds = 0\n",
            r"\[llm\]\.timeout_seconds: значение должно быть не меньше 1",
        ),
        (
            "[agent]\nmax_llm_steps = 9\n",
            r"\[agent\]\.max_llm_steps: значение должно быть не больше 8",
        ),
    ],
)
def test_toml_type_choice_and_range_errors_name_the_parameter(
    tmp_path: Path,
    config_text: str,
    expected: str,
) -> None:
    with pytest.raises(ConfigError, match=expected):
        settings_from(tmp_path, config_text=config_text)


def test_toml_does_not_accept_secrets_or_credentials(tmp_path: Path) -> None:
    secret = "super-secret-value"

    with pytest.raises(ConfigError, match=r"\[llm\]\.api_key: неизвестный параметр") as error:
        settings_from(tmp_path, config_text=f'[llm]\napi_key = "{secret}"\n')

    assert secret not in str(error.value)

    with pytest.raises(ConfigError, match=r"\[llm\]\.base_url: не храните учётные данные") as error:
        settings_from(
            tmp_path,
            config_text='[llm]\nbase_url = "https://user:password@provider.test/v1"\n',
        )

    assert "password" not in str(error.value)


def test_malformed_toml_is_a_safe_config_error(tmp_path: Path) -> None:
    secret = "private-value"
    with pytest.raises(ConfigError, match="не удалось прочитать или разобрать") as error:
        settings_from(tmp_path, config_text=f"[llm\nmodel = '{secret}'\n")

    assert secret not in str(error.value)


def test_missing_config_file_keeps_code_defaults(tmp_path: Path) -> None:
    settings = Settings.load(
        tmp_path / "missing.env",
        environ={"BOT_TOKEN": TOKEN, "POSTGRES_PASSWORD": "unused"},
        config_file=tmp_path / "missing.toml",
    )

    assert settings.llm_model == "openai/gpt-4o-mini"
    assert settings.llm_max_tokens == 16384
    assert settings.llm_generation_reserve_tokens == 4096
    assert settings.default_context_length == 32768
    assert settings.math_mcp_request_timeout_seconds == 40.0


def test_settings_collect_only_math_mcp_overrides_from_env(tmp_path: Path) -> None:
    config_text = """
[math_mcp]
startup_timeout_seconds = 31.0
operation_timeout_seconds = 6.0
queue_timeout_seconds = 7.0
"""
    env_text = (
        "MATH_MCP_OPERATION_TIMEOUT_SECONDS=5.5\n"
        "MATH_MCP_CONFIG_FILE=./config.math.toml\n"
        "LLM_API_KEY=private-key\n"
    )

    settings = settings_from(
        tmp_path,
        config_text=config_text,
        env_text=env_text,
        environ={"MATH_MCP_MEMORY_LIMIT_MIB": "900"},
    )

    assert settings.math_mcp_environment == {
        "MATH_MCP_OPERATION_TIMEOUT_SECONDS": "5.5",
        "MATH_MCP_CONFIG_FILE": "./config.math.toml",
        "MATH_MCP_MEMORY_LIMIT_MIB": "900",
    }
    assert "MATH_MCP_STARTUP_TIMEOUT_SECONDS" not in settings.math_mcp_environment
    assert "LLM_API_KEY" not in settings.math_mcp_environment
    assert "private-key" not in repr(settings)
    assert "private-key" not in str(settings.math_mcp_environment)


def test_math_stdio_client_receives_only_configured_environment_overrides(
    tmp_path: Path,
) -> None:
    from fastmcp.client.transports import StdioTransport

    from app.mcp.connection import create_math_tools_client

    env_text = (
        "MATH_MCP_OPERATION_TIMEOUT_SECONDS=2.5\n"
        "MATH_MCP_CONFIG_FILE=./config.math.toml\n"
        "LLM_API_KEY=private-key\n"
    )
    settings = settings_from(
        tmp_path,
        env_text=env_text,
        environ={"MATH_MCP_MEMORY_LIMIT_MIB": "512"},
    )
    client = create_math_tools_client(settings)
    assert client is not None

    transport = client._client.transport
    assert isinstance(transport, StdioTransport)
    child_environment = transport.env or {}

    assert child_environment["MATH_MCP_OPERATION_TIMEOUT_SECONDS"] == "2.5"
    assert child_environment["MATH_MCP_CONFIG_FILE"] == "./config.math.toml"
    assert child_environment["MATH_MCP_MEMORY_LIMIT_MIB"] == "512"
    assert "LLM_API_KEY" not in child_environment
    assert "private-key" not in child_environment.values()


def test_empty_process_value_inherits_dotenv_before_toml(tmp_path: Path) -> None:
    config_text = """
[math_mcp]
operation_timeout_seconds = 6.0
"""
    env_text = "MATH_MCP_OPERATION_TIMEOUT_SECONDS=3.5\n"

    from_dotenv = math_settings_from(
        tmp_path,
        config_text=config_text,
        env_text=env_text,
        environ={"MATH_MCP_OPERATION_TIMEOUT_SECONDS": ""},
    )
    from_toml = math_settings_from(
        tmp_path,
        config_text=config_text,
        environ={"MATH_MCP_OPERATION_TIMEOUT_SECONDS": ""},
    )

    assert from_dotenv.math_mcp_operation_timeout_seconds == 3.5
    assert from_toml.math_mcp_operation_timeout_seconds == 6.0


def test_math_settings_load_limits_from_toml_and_environment(tmp_path: Path) -> None:
    config_text = """
[mcp]
transport = "stdio"

[math_mcp]
startup_timeout_seconds = 31.0
operation_timeout_seconds = 6.0
queue_timeout_seconds = 7.0
memory_limit_mib = 900
max_png_bytes = 200000
"""
    loaded = math_settings_from(tmp_path, config_text=config_text)
    overridden = math_settings_from(
        tmp_path,
        config_text=config_text,
        environ={"MATH_MCP_OPERATION_TIMEOUT_SECONDS": "2.5"},
    )

    assert loaded.math_mcp_startup_timeout_seconds == 31.0
    assert loaded.math_mcp_operation_timeout_seconds == 6.0
    assert loaded.math_mcp_queue_timeout_seconds == 7.0
    assert loaded.math_mcp_memory_limit_mib == 900
    assert loaded.math_mcp_max_png_bytes == 200000
    assert overridden.math_mcp_operation_timeout_seconds == 2.5


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("MATH_MCP_STARTUP_TIMEOUT_SECONDS", "0", "MATH_MCP_STARTUP_TIMEOUT_SECONDS"),
        ("MATH_MCP_OPERATION_TIMEOUT_SECONDS", "text", "MATH_MCP_OPERATION_TIMEOUT_SECONDS"),
        ("MATH_MCP_QUEUE_TIMEOUT_SECONDS", "181", "MATH_MCP_QUEUE_TIMEOUT_SECONDS"),
        ("MATH_MCP_MEMORY_LIMIT_MIB", "64", "MATH_MCP_MEMORY_LIMIT_MIB"),
        ("MATH_MCP_MAX_PNG_BYTES", "2048.5", "MATH_MCP_MAX_PNG_BYTES"),
    ],
)
def test_math_settings_reject_invalid_limit_values(
    tmp_path: Path,
    key: str,
    value: str,
    expected: str,
) -> None:
    with pytest.raises(MathConfigError, match=expected):
        math_settings_from(tmp_path, environ={key: value})


def test_packaged_math_timeouts_form_a_usable_profile(tmp_path: Path) -> None:
    settings = Settings.load(
        tmp_path / "missing.env",
        environ={"BOT_TOKEN": TOKEN, "POSTGRES_PASSWORD": "unused"},
        config_file=ROOT / "config.toml",
    )
    math = MathSettings.load(
        tmp_path / "missing.env",
        environ={},
        config_file=ROOT / "config.toml",
    )

    assert (
        settings.math_mcp_request_timeout_seconds
        >= math.math_mcp_startup_timeout_seconds + math.math_mcp_operation_timeout_seconds
    )
    assert math.math_mcp_queue_timeout_seconds <= settings.math_mcp_request_timeout_seconds
    assert settings.effective_generation_reserve_tokens <= settings.llm_max_tokens
    assert math.math_mcp_memory_limit_mib == 768
    assert math.math_mcp_max_png_bytes == 256 * 1024


def test_diagnostics_show_effective_values_without_secrets() -> None:
    settings = Settings(
        bot_token=TOKEN,
        postgres_password="database-password",
        telegram_proxy_url="http://proxy-user:proxy-password@proxy.test:3128",
        llm_api_key="llm-key",
        llm_base_url="https://api-key:llm-key@provider.test/v1",
        mcp_shared_secret="m" * 40,
        math_mcp_server_url="http://mcp-user:mcp-password@math.test/mcp/",
    )

    diagnostics = settings.diagnostics()
    rendered = str(diagnostics)

    for secret in (
        TOKEN,
        "database-password",
        "proxy-password",
        "llm-key",
        "m" * 40,
        "mcp-password",
    ):
        assert secret not in rendered

    assert diagnostics["llm_generation_reserve_tokens"] == 4096
    assert diagnostics["llm_effective_generation_reserve_tokens"] == 4096
    assert diagnostics["math_mcp_request_timeout_seconds"] == 40.0
    assert public_url(settings.llm_base_url) == "https://provider.test"
    assert public_url("https://provider.test/v1?token=secret#fragment") == "https://provider.test"
    assert public_url("https://[::1]:8443/v1?token=secret") == "https://[::1]:8443"
    assert public_url("not a url") == "<invalid>"


def test_toml_public_urls_reject_query_fragment_and_malformed_values(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"\[llm\]\.base_url: не храните параметры запроса"):
        settings_from(
            tmp_path,
            config_text='[llm]\nbase_url = "https://provider.test/v1?token=secret"\n',
        )

    with pytest.raises(ConfigError, match=r"\[mcp\]\.server_url: укажите корректный URL"):
        settings_from(tmp_path, config_text='[mcp]\nserver_url = "http://[::1"\n')


def test_math_worker_environment_receives_resolved_limits() -> None:
    from mcp_servers.math_tools.runtime import _worker_environment

    environment = _worker_environment(512, 2.5)

    assert environment["MATH_MCP_OPERATION_TIMEOUT_SECONDS"] == "2.5"
    assert environment["MATH_MCP_MEMORY_LIMIT_MIB"] == "512"


def test_math_settings_reject_png_limit_above_existing_contract(tmp_path: Path) -> None:
    with pytest.raises(MathConfigError, match="MATH_MCP_MAX_PNG_BYTES"):
        math_settings_from(tmp_path, environ={"MATH_MCP_MAX_PNG_BYTES": "524288"})
