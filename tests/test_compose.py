import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.config import ConfigError, MCPTransport
from mcp_servers.student_tools.config import ServerSettings
from scripts.common import compose_quote


def copy_compose_project(tmp_path: Path) -> Path:
    root = Path(__file__).resolve().parents[1]
    project = tmp_path / "project"
    project.mkdir()

    for name in ("compose.yaml", "compose.local.yaml", "compose.config.yaml"):
        shutil.copy(root / name, project / name)

    return project


def compose_environment(**updates: str) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("POSTGRES_", "BOT_TOKEN", "COMPOSE_", "MATH_MCP_", "LLM_"))
    }
    environment.update(updates)
    return environment


def run_compose_config(command: list[str], environment: dict[str, str]) -> dict:
    result = subprocess.run(
        [*command, "config", "--format", "json"],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, "Docker Compose не смог разобрать конфигурацию"
    return json.loads(result.stdout)["services"]


@pytest.mark.skipif(not shutil.which("docker"), reason="Docker CLI не установлен")
def test_local_and_cloud_compose_ports_credentials_and_healthchecks(tmp_path):
    project = copy_compose_project(tmp_path)
    password = "p$a#s's\\\\tail"
    local_env = tmp_path / ".env"
    local_env.write_text(
        "BOT_TOKEN=test\nPOSTGRES_PORT=55432\nPOSTGRES_PASSWORD=" + compose_quote(password) + "\n",
        encoding="utf-8",
    )
    local = run_compose_config(
        [
            "docker",
            "compose",
            "--project-name",
            "itmo-test",
            "--project-directory",
            str(project),
            "--env-file",
            str(local_env),
            "-f",
            str(project / "compose.yaml"),
            "-f",
            str(project / "compose.local.yaml"),
        ],
        compose_environment(),
    )

    cloud_env = project / ".env.cloud"
    cloud_env.write_text(
        "\n".join(
            [
                "BOT_TOKEN=test",
                "POSTGRES_PASSWORD=test",
                "LLM_API_KEY=synthetic-llm-key",
                "MCP_SHARED_SECRET=" + "m" * 40,
                "MATH_MCP_OPERATION_TIMEOUT_SECONDS=2.5",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    assert not (project / ".env").exists()
    cloud = run_compose_config(
        [
            "docker",
            "compose",
            "--env-file",
            str(cloud_env),
            "-f",
            str(project / "compose.yaml"),
            "--profile",
            "cloud",
        ],
        compose_environment(COMPOSE_ENV_FILE=".env.cloud"),
    )

    assert local["db"]["ports"][0]["host_ip"] == "127.0.0.1"
    assert local["db"]["ports"][0]["published"] == "55432"
    assert local["db"]["environment"]["POSTGRES_PASSWORD"].replace("$$", "$") == password
    assert all(not service.get("ports") for service in cloud.values())
    assert cloud["bot"]["environment"]["POSTGRES_HOST"] == "db"
    assert cloud["bot"]["environment"]["POSTGRES_PORT"] == "5432"
    assert cloud["bot"]["environment"]["MCP_SERVER_URL"] == "http://student-tools-mcp:8000/mcp"
    assert cloud["bot"]["environment"]["MATH_MCP_SERVER_URL"] == "http://math-tools-mcp:8000/mcp"
    assert cloud["bot"]["environment"]["LLM_API_KEY"] == "synthetic-llm-key"
    assert cloud["bot"]["image"] == cloud["student-tools-mcp"]["image"] == "itmo-ai-bot:local"
    assert cloud["bot"]["image"] == cloud["math-tools-mcp"]["image"]
    assert cloud["bot"]["build"] == cloud["student-tools-mcp"]["build"]
    assert cloud["bot"]["build"] == cloud["math-tools-mcp"]["build"]
    assert cloud["student-tools-mcp"]["command"] == [
        "python",
        "-m",
        "mcp_servers.student_tools",
        "--transport",
        "streamable-http",
    ]
    assert cloud["math-tools-mcp"]["command"] == [
        "python",
        "-m",
        "mcp_servers.math_tools",
        "--transport",
        "streamable-http",
    ]
    assert "BOT_TOKEN" not in cloud["student-tools-mcp"]["environment"]
    assert "BOT_TOKEN" not in cloud["math-tools-mcp"]["environment"]
    assert "env_file" not in cloud["student-tools-mcp"]
    assert "env_file" not in cloud["math-tools-mcp"]
    assert not cloud["student-tools-mcp"].get("ports")
    assert not cloud["math-tools-mcp"].get("ports")
    assert cloud["student-tools-mcp"]["depends_on"]["db"]["condition"] == "service_healthy"
    assert "depends_on" not in cloud["math-tools-mcp"]
    assert cloud["bot"]["depends_on"].keys() == {"db"}
    assert "mcp_internal" in cloud["student-tools-mcp"]["networks"]
    assert "mcp_internal" in cloud["bot"]["networks"]
    assert list(cloud["math-tools-mcp"]["networks"]) == ["mcp_internal"]
    assert (
        cloud["student-tools-mcp"]["healthcheck"]["test"][-1]
        == "mcp_servers.student_tools.healthcheck"
    )
    assert (
        cloud["math-tools-mcp"]["healthcheck"]["test"][-1] == "mcp_servers.math_tools.healthcheck"
    )
    math_environment = cloud["math-tools-mcp"]["environment"]
    assert (
        math_environment["MCP_SHARED_SECRET"]
        == (cloud["student-tools-mcp"]["environment"]["MCP_SHARED_SECRET"])
    )
    assert all(not key.startswith("POSTGRES_") for key in math_environment)
    assert math_environment["MCP_PORT"] == "8000"
    assert math_environment["MATH_MCP_OPERATION_TIMEOUT_SECONDS"] == "2.5"
    assert math_environment["MATH_MCP_STARTUP_TIMEOUT_SECONDS"] is None
    assert math_environment["MATH_MCP_QUEUE_TIMEOUT_SECONDS"] is None
    assert math_environment["MATH_MCP_MEMORY_LIMIT_MIB"] is None
    assert math_environment["MATH_MCP_MAX_PNG_BYTES"] is None
    assert "volumes" not in cloud["bot"]
    assert "volumes" not in cloud["math-tools-mcp"]
    schedule_mount = cloud["student-tools-mcp"]["volumes"][0]
    assert schedule_mount["type"] == "bind"
    assert schedule_mount["target"] == "/app/mcp_servers/student_tools/data/schedule.json"
    assert schedule_mount["read_only"] is True
    assert (
        cloud["student-tools-mcp"]["environment"]["SCHEDULE_PATH"]
        == "mcp_servers/student_tools/data/schedule.json"
    )
    assert "app.healthcheck" in cloud["bot"]["healthcheck"]["test"]
    assert cloud["bot"]["healthcheck"]["timeout"] == "20s"
    assert cloud["bot"]["depends_on"]["db"]["condition"] == "service_healthy"


@pytest.mark.skipif(not shutil.which("docker"), reason="Docker CLI не установлен")
def test_cloud_compose_without_any_local_dotenv_uses_optional_env_file(tmp_path):
    project = copy_compose_project(tmp_path)
    env_file = tmp_path / "synthetic.env"
    env_file.write_text(
        "BOT_TOKEN=test\nPOSTGRES_PASSWORD=test\nLLM_API_KEY=synthetic-key\n",
        encoding="utf-8",
    )
    assert not (project / ".env").exists()

    services = run_compose_config(
        [
            "docker",
            "compose",
            "--env-file",
            str(env_file),
            "-f",
            str(project / "compose.yaml"),
            "--profile",
            "cloud",
        ],
        compose_environment(),
    )

    assert services["bot"]["environment"]["POSTGRES_HOST"] == "db"


@pytest.mark.skipif(not shutil.which("docker"), reason="Docker CLI не установлен")
def test_optional_compose_profile_mounts_separate_configs(tmp_path):
    project = copy_compose_project(tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "BOT_TOKEN=test\nPOSTGRES_PASSWORD=test\n",
        encoding="utf-8",
    )
    bot_config = tmp_path / "bot.toml"
    math_config = tmp_path / "math.toml"
    bot_config.write_text("[llm]\n", encoding="utf-8")
    math_config.write_text("[math_mcp]\n", encoding="utf-8")

    services = run_compose_config(
        [
            "docker",
            "compose",
            "--env-file",
            str(env_file),
            "-f",
            str(project / "compose.yaml"),
            "-f",
            str(project / "compose.config.yaml"),
            "--profile",
            "cloud",
        ],
        compose_environment(
            BOT_CONFIG_FILE=str(bot_config),
            MATH_MCP_CONFIG_FILE=str(math_config),
        ),
    )

    assert services["bot"]["configs"] == [{"source": "bot-config", "target": "/app/config.toml"}]
    assert services["math-tools-mcp"]["configs"] == [
        {"source": "math-config", "target": "/app/config.toml"}
    ]


def test_server_settings_loads_without_bot_token(tmp_path):
    settings = ServerSettings.load(
        tmp_path / "missing.env",
        environ={"POSTGRES_PASSWORD": "database", "MCP_SHARED_SECRET": "s" * 32},
    )

    assert settings.postgres_password == "database"
    assert settings.mcp_shared_secret == "s" * 32
    assert settings.mcp_transport == MCPTransport.STDIO
    assert settings.mcp_port == 8000
    assert settings.schedule_path == Path("mcp_servers/student_tools/data/schedule.json")
    assert "database" not in repr(settings)
    assert "s" * 32 not in repr(settings)


@pytest.mark.parametrize(
    ("updates", "error_name"),
    [
        ({"MCP_SHARED_SECRET": "short"}, "MCP_SHARED_SECRET"),
        ({"SCHEDULE_TIMEZONE": "No/Such_Zone"}, "SCHEDULE_TIMEZONE"),
        ({"MCP_PORT": "70000"}, "MCP_PORT"),
        ({"POSTGRES_PORT": "zero"}, "POSTGRES_PORT"),
        ({"MCP_TRANSPORT": "sse"}, "MCP_TRANSPORT"),
    ],
)
def test_server_settings_reject_invalid_configuration(tmp_path, updates, error_name):
    environ = {"POSTGRES_PASSWORD": "database", "MCP_SHARED_SECRET": "s" * 32, **updates}

    with pytest.raises(ConfigError, match=error_name):
        ServerSettings.load(tmp_path / "missing.env", environ=environ)


def test_server_settings_requires_database_password_and_secret(tmp_path):
    with pytest.raises(ConfigError, match="POSTGRES_PASSWORD"):
        ServerSettings.load(
            tmp_path / "missing.env",
            environ={"MCP_SHARED_SECRET": "s" * 32},
        )

    with pytest.raises(ConfigError, match="MCP_SHARED_SECRET") as error:
        ServerSettings.load(tmp_path / "missing.env", environ={"POSTGRES_PASSWORD": "database"})

    assert "database" not in str(error.value)
