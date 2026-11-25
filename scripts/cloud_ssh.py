import base64
import ipaddress
import tarfile
import time
from dataclasses import asdict
from pathlib import Path
from typing import Protocol

from app.config import MATH_MCP_ENVIRONMENT_KEYS, Settings
from scripts.common import CommandError, compose_quote, run_command

REMOTE = "/home/student/course-bot"
COMPOSE = (
    f"cd {REMOTE} && COMPOSE_ENV_FILE=.env.cloud "
    "docker compose --env-file .env.cloud --profile cloud"
)


class SSHCommand(Protocol):
    def __call__(
        self,
        instance: dict,
        command: str,
        *,
        timeout: int = 120,
        input_text: str | None = None,
        stream_output: bool = False,
    ) -> str: ...


def ssh(
    directory: Path,
    instance: dict,
    command: str,
    *,
    timeout: int = 120,
    input_text: str | None = None,
    stream_output: bool = False,
) -> str:
    try:
        address = instance["network_interfaces"][0]["primary_v4_address"]["one_to_one_nat"][
            "address"
        ]
        ipaddress.IPv4Address(address)
    except (KeyError, IndexError, ValueError):
        raise CommandError("У ВМ нет публичного IPv4. Проверьте её запуск.") from None

    key = directory / "id_ed25519"
    if not key.exists():
        raise CommandError("Утерян SSH-ключ .deploy/id_ed25519. Восстановите его из своей копии.")

    return run_command(
        [
            "ssh",
            "-i",
            str(key),
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            f'UserKnownHostsFile="{(directory / "known_hosts").as_posix()}"',
            "-o",
            "ConnectTimeout=10",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=3",
            f"student@{address}",
            command,
        ],
        label="SSH: проверьте IP/CIDR, ключ, cloud-init и доступность ВМ",
        timeout=timeout,
        input_text=input_text,
        interactive=stream_output,
    )


def wait_for_ssh(ssh: SSHCommand, instance: dict) -> None:
    deadline = time.monotonic() + 600

    while time.monotonic() < deadline:
        try:
            ssh(instance, "true", timeout=20)
            return
        except CommandError:
            print("Ждём SSH на ВМ…", flush=True)
            time.sleep(5)

    raise CommandError("SSH не стал доступен за 10 минут; проверьте --ssh-cidr и сеть.")


def deploy(root: Path, ssh: SSHCommand, instance: dict, settings: Settings) -> None:
    print("Ждём установки Docker на ВМ…", flush=True)
    ssh(
        instance,
        "sudo cloud-init status --wait && docker compose version",
        timeout=900,
        stream_output=True,
    )

    _upload_sources(root, ssh, instance)
    _upload_environment(ssh, instance, settings)

    print("Собираем приложение и проверяем его готовность…", flush=True)
    ssh(instance, _deployment_command(), timeout=1200, stream_output=True)

    print(ssh(instance, f"{COMPOSE} exec -T bot python -m app.healthcheck"))
    print("Деплой завершён. Статус и логи: deploy status / deploy logs.")


def _upload_sources(root: Path, ssh: SSHCommand, instance: dict) -> None:
    archive = source_archive(root)

    try:
        payload = base64.b64encode(archive.read_bytes()).decode("ascii")
        ssh(
            instance,
            "umask 077; base64 -d > /home/student/course-source.tar.gz",
            input_text=payload,
            timeout=180,
        )
    finally:
        archive.unlink(missing_ok=True)


def _upload_environment(ssh: SSHCommand, instance: dict, settings: Settings) -> None:
    content = _environment_file_content(settings)
    payload = base64.b64encode(content.encode("utf-8")).decode("ascii")

    ssh(
        instance,
        "umask 077; base64 -d > /home/student/course-env.pending",
        input_text=payload,
    )


def _deployment_environment(settings: Settings) -> dict[str, str]:
    values = {
        key.upper(): str(value)
        for key, value in asdict(settings).items()
        if not isinstance(value, dict)
    }
    values.update(POSTGRES_HOST="db", POSTGRES_PORT="5432")

    if ".local" in Path(values["SCHEDULE_PATH"]).parts:
        values["SCHEDULE_PATH"] = "mcp_servers/student_tools/data/schedule.json"

    for key, value in settings.math_mcp_environment.items():
        if key in MATH_MCP_ENVIRONMENT_KEYS and key != "MATH_MCP_CONFIG_FILE":
            values[key] = value

    return values


def _environment_file_content(settings: Settings) -> str:
    values = _deployment_environment(settings)
    return "".join(f"{key}={compose_quote(value)}\n" for key, value in values.items())


def _deployment_command() -> str:
    stop_services = "".join(
        f"if printf '%s\\n' \"$services\" | grep -qx {service}; then {COMPOSE} stop {service}; fi; "
        for service in ("bot", "mcp-server", "student-tools-mcp", "math-tools-mcp")
    )
    return (
        "set -eu; "
        f"mkdir -p {REMOTE}; chmod 700 {REMOTE}; "
        f"if [ -f {REMOTE}/.env.cloud ]; then "
        f"services=$({COMPOSE} config --services); "
        f"{stop_services}fi; "
        f"rm -rf {REMOTE}/app {REMOTE}/mcp_servers; "
        f"tar -xzf /home/student/course-source.tar.gz -C {REMOTE}; "
        f"mv /home/student/course-env.pending {REMOTE}/.env.cloud; "
        f"chmod 600 {REMOTE}/.env.cloud; "
        "rm /home/student/course-source.tar.gz; "
        f"{COMPOSE} up -d --build --remove-orphans --wait --wait-timeout 180"
    )


def source_archive(root: Path) -> Path:
    deployment = root / ".deploy"
    deployment.mkdir(exist_ok=True)
    archive_path = deployment / "source.tar.gz"

    candidates = [
        root / name
        for name in [
            "Dockerfile",
            ".dockerignore",
            "pyproject.toml",
            "uv.lock",
            ".python-version",
            "compose.yaml",
            "compose.config.yaml",
            "config.toml",
        ]
    ]
    candidates.extend((root / "app").rglob("*.py"))
    candidates.extend((root / "app" / "db" / "migrations" / "sql").glob("*.sql"))
    candidates.extend((root / "mcp_servers").rglob("*.py"))
    candidates.extend((root / "mcp_servers").glob("*/data/*.json"))

    with tarfile.open(archive_path, "w:gz") as archive:
        for path in sorted(candidates):
            if (
                path.is_file()
                and "__pycache__" not in path.parts
                and ".local" not in path.parts
                and not path.name.startswith(".env")
                and not path.is_symlink()
            ):
                archive.add(
                    path,
                    arcname=path.relative_to(root).as_posix(),
                    recursive=False,
                )

    return archive_path
