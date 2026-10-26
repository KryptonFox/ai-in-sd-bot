import subprocess
from unittest.mock import Mock

import pytest
from dotenv import dotenv_values

from scripts.common import CommandError, run_command
from scripts.local import compose_command, prepare_env


def test_first_setup_and_repeat_preserve_secrets(tmp_path):
    root = tmp_path / "Курс с пробелами"
    root.mkdir()
    prompt = Mock(side_effect=["123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk", ""])

    path = prepare_env(root, cloud=False, ask=prompt, environ={})
    first = path.read_bytes()

    prepare_env(root, cloud=False, ask=Mock(side_effect=AssertionError), environ={})

    assert path.read_bytes() == first
    assert len(dotenv_values(path)["POSTGRES_PASSWORD"]) >= 24
    assert dotenv_values(path)["POSTGRES_HOST"] == "127.0.0.1"


def test_cloud_requires_proxy_and_has_separate_password(tmp_path):
    token = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk"
    prepare_env(tmp_path, cloud=False, ask=Mock(side_effect=[token, ""]), environ={})

    path = prepare_env(
        tmp_path,
        cloud=True,
        ask=Mock(side_effect=[token, "socks5://user:p%40ss@proxy:1080"]),
        environ={},
    )
    config = dotenv_values(path)

    assert config["POSTGRES_HOST"] == "db"
    assert config["POSTGRES_PASSWORD"] != dotenv_values(tmp_path / ".env")["POSTGRES_PASSWORD"]
    assert config["TELEGRAM_PROXY_URL"] == "socks5://user:p%40ss@proxy:1080"
    assert len(config["MCP_SHARED_SECRET"]) >= 32

    first = path.read_bytes()
    prepare_env(tmp_path, cloud=True, ask=Mock(side_effect=AssertionError), environ={})
    assert path.read_bytes() == first


def test_cloud_persists_environment_mcp_secret_and_rejects_short_secret(tmp_path):
    env = {
        "BOT_TOKEN": "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk",
        "POSTGRES_PASSWORD": "database-password",
        "TELEGRAM_PROXY_URL": "socks5://proxy:1080",
        "MCP_SHARED_SECRET": "m" * 40,
    }
    path = prepare_env(tmp_path, cloud=True, ask=Mock(), environ=env)

    assert dotenv_values(path)["MCP_SHARED_SECRET"] == "m" * 40
    assert path.stat().st_mode & 0o077 == 0

    with pytest.raises(ValueError, match="MCP_SHARED_SECRET"):
        prepare_env(
            tmp_path / "short",
            cloud=True,
            ask=Mock(),
            environ={**env, "MCP_SHARED_SECRET": "short"},
        )


def test_cloud_empty_proxy_fails_before_creating_file(tmp_path):
    with pytest.raises(ValueError, match="прокси"):
        prepare_env(
            tmp_path,
            cloud=True,
            ask=Mock(side_effect=["123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk", ""]),
            environ={},
        )

    assert not (tmp_path / ".env.cloud").exists()


def test_compose_uses_absolute_paths(tmp_path):
    root = tmp_path / "Проект с пробелами"

    command = compose_command(root)

    assert str(root / "compose.yaml") in command
    assert str(root / ".env") in command
    assert command[:2] == ["docker", "compose"]
    assert command[command.index("--project-name") + 1].isascii()


@pytest.mark.parametrize("failure", [FileNotFoundError(), subprocess.TimeoutExpired("secret", 1)])
def test_command_failure_has_safe_message(monkeypatch, failure):
    monkeypatch.setattr(subprocess, "run", Mock(side_effect=failure))

    with pytest.raises(CommandError) as exc:
        run_command(["tool", "secret"], label="Проверка инструмента")

    assert "secret" not in str(exc.value)
    assert "Проверка инструмента" in str(exc.value)


def test_command_stderr_not_leaked(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        Mock(return_value=subprocess.CompletedProcess(["tool"], 1, "secret-out", "secret-error")),
    )

    with pytest.raises(CommandError) as exc:
        run_command(["tool"], label="Ошибка инструмента")

    assert "secret" not in str(exc.value)


def test_command_can_show_prefixed_safe_error(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        Mock(
            return_value=subprocess.CompletedProcess(
                ["tool"], 1, "secret-out", "debug details\nERROR: quota exceeded\n"
            )
        ),
    )

    with pytest.raises(CommandError, match="ERROR: quota exceeded"):
        run_command(["tool"], label="Ошибка инструмента", error_prefix="ERROR:")


class FakeYC:
    def __init__(self):
        self.resources = {}
        self.created = []
        self.deleted = []
        self.fail_after_create = None

    def __call__(self, args, **kwargs):
        kind = tuple(args[:2])
        operation = args[2]

        if operation == "list":
            return list(self.resources.get(kind, {}).values())

        if operation == "create":
            name = args[args.index("--name") + 1]
            label = args[args.index("--labels") + 1].split("=", 1)[1]
            resource = {
                "id": f"id-{len(self.created)}",
                "name": name,
                "folder_id": "folder-1",
                "labels": {"template-id": label},
            }
            self.resources.setdefault(kind, {})[resource["id"]] = resource
            self.created.append(kind)

            if self.fail_after_create == kind:
                self.fail_after_create = None
                raise CommandError("Соединение прервано после создания")

            return resource

        if operation == "get":
            return self.resources[kind][args[args.index("--id") + 1]]

        if operation == "delete":
            rid = args[args.index("--id") + 1]
            del self.resources[kind][rid]
            self.deleted.append((kind, rid))
            return {}

        raise AssertionError(args)


def cloud(tmp_path, fake):
    from scripts.cloud import Cloud

    return Cloud(tmp_path, folder_id="folder-1", yc=fake)


def test_cloud_reuses_resources_after_restart(tmp_path):
    fake = FakeYC()
    first = cloud(tmp_path, fake)
    original = first.ensure("network", ["vpc", "network"], [])

    resumed = cloud(tmp_path, fake)
    again = resumed.ensure("network", ["vpc", "network"], [])

    assert again["id"] == original["id"]
    assert len(fake.created) == 1


def test_cloud_recovers_ambiguous_creation(tmp_path):
    fake = FakeYC()
    fake.fail_after_create = ("vpc", "network")
    deployment = cloud(tmp_path, fake)

    with pytest.raises(CommandError):
        deployment.ensure("network", ["vpc", "network"], [])

    resumed = cloud(tmp_path, fake)
    found = resumed.ensure("network", ["vpc", "network"], [])

    assert found["id"] == "id-0"
    assert len(fake.created) == 1


def test_cloud_refuses_foreign_resource_and_folder(tmp_path):
    fake = FakeYC()
    deployment = cloud(tmp_path, fake)
    item = deployment.ensure("network", ["vpc", "network"], [])
    item["labels"] = {"template-id": "someone-else"}

    with pytest.raises(CommandError, match="принадлеж"):
        deployment.destroy(confirmed=True)

    assert not fake.deleted

    with pytest.raises(CommandError, match="каталог"):
        from scripts.cloud import Cloud

        Cloud(tmp_path, folder_id="another-folder", yc=fake)


def test_delete_requires_confirmation_and_only_deletes_owned_ids(tmp_path):
    fake = FakeYC()
    deployment = cloud(tmp_path, fake)
    item = deployment.ensure("network", ["vpc", "network"], [])
    fake.resources[("vpc", "network")]["foreign"] = {
        "id": "foreign",
        "name": "foreign",
        "labels": {},
        "folder_id": "folder-1",
    }

    with pytest.raises(CommandError, match="подтверждение"):
        deployment.destroy(confirmed=False)

    deployment.destroy(confirmed=True)

    assert fake.deleted == [(("vpc", "network"), item["id"])]
    assert "foreign" in fake.resources[("vpc", "network")]


def test_cloud_reuses_default_network_and_does_not_delete_it(tmp_path):
    from scripts.cli import parser

    fake = FakeYC()
    default_network = {
        "id": "default-network-id",
        "name": "default",
        "labels": {},
        "folder_id": "folder-1",
    }
    fake.resources[("vpc", "network")] = {default_network["id"]: default_network}

    deployment = cloud(tmp_path, fake)
    deployment.state["resources"]["network"] = {"name": f"itmo-{deployment.state['owner']}-network"}
    deployment.save()

    key = tmp_path / ".deploy" / "id_ed25519"
    key.write_text("test-private-key", encoding="utf-8")
    key.with_suffix(".pub").write_text("ssh-ed25519 test-public-key", encoding="utf-8")

    args = parser().parse_args(["cloud", "up", "--ssh-cidr", "203.0.113.10/32"])
    deployment.provision(args)
    deployment.destroy(confirmed=True)

    assert fake.created == [
        ("vpc", "subnet"),
        ("vpc", "security-group"),
        ("compute", "instance"),
    ]
    assert default_network["id"] in fake.resources[("vpc", "network")]
    assert all(kind != ("vpc", "network") for kind, _ in fake.deleted)


def test_deploy_archive_never_contains_env_or_keys(tmp_path):
    import tarfile

    from scripts.cloud import source_archive

    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("", encoding="utf-8")
    for name in ["Dockerfile", ".dockerignore", "pyproject.toml", "uv.lock", ".python-version"]:
        (tmp_path / name).write_text("test", encoding="utf-8")
    migrations = tmp_path / "app" / "db" / "migrations" / "sql"
    migrations.mkdir(parents=True)
    (migrations / "0001_initial_schema.sql").write_text("SELECT 1;", encoding="utf-8")
    data = tmp_path / "app" / "data"
    data.mkdir()
    (data / "schedule.json").write_text("[]", encoding="utf-8")
    (tmp_path / "app" / ".env").write_text("SECRET=yes", encoding="utf-8")
    (tmp_path / ".env.cloud").write_text("SECRET=yes", encoding="utf-8")
    (tmp_path / ".deploy").mkdir()
    (tmp_path / ".deploy" / "id_ed25519").write_text("PRIVATE", encoding="utf-8")

    path = source_archive(tmp_path)

    with tarfile.open(path) as archive:
        names = archive.getnames()

    assert "app/__init__.py" in names
    assert {
        "Dockerfile",
        ".dockerignore",
        "pyproject.toml",
        "uv.lock",
        ".python-version",
        "app/db/migrations/sql/0001_initial_schema.sql",
        "app/data/schedule.json",
    } <= set(names)
    assert all(".env" not in n and "id_ed25519" not in n for n in names)


def test_missing_cloud_key_is_not_replaced(tmp_path):
    deployment = cloud(tmp_path, FakeYC())
    deployment.state["resources"]["vm"] = {"id": "existing", "name": "existing"}

    with pytest.raises(CommandError, match="ключ"):
        deployment.key()

    assert not (tmp_path / ".deploy" / "id_ed25519").exists()


def test_provision_builds_private_network_and_nonpreemptible_vm(tmp_path, monkeypatch):
    from scripts.cli import parser

    fake = FakeYC()
    deployment = cloud(tmp_path, fake)
    key = tmp_path / ".deploy" / "id_ed25519"
    key.write_text("test-private-key", encoding="utf-8")
    key.with_suffix(".pub").write_text("ssh-ed25519 test-public-key", encoding="utf-8")

    calls = []
    original = deployment.yc

    def recording(args):
        calls.append(args)
        return original(args)

    deployment.yc = recording
    args = parser().parse_args(["cloud", "up", "--ssh-cidr", "203.0.113.10/32"])
    deployment.provision(args)
    deployment.provision(args)

    assert len(fake.created) == 4

    vm = next(c for c in calls if c[:3] == ["compute", "instance", "create"])
    assert "--preemptible" not in vm
    assert vm[vm.index("--core-fraction") + 1] == "5"
    assert "auto-delete=true" in vm[vm.index("--create-boot-disk") + 1]

    group = next(c for c in calls if c[:3] == ["vpc", "security-group", "create"])
    assert "direction=ingress,protocol=tcp,port=22,v4-cidrs=203.0.113.10/32" in group
    assert "direction=egress,protocol=any,port=any,v4-cidrs=0.0.0.0/0" in group

    initialization = (tmp_path / ".deploy" / "cloud-init.yaml").read_text(encoding="utf-8")
    assert "test-private-key" not in initialization
    assert "BOT_TOKEN" not in initialization
    assert "swapfile" in initialization


def test_deploy_uploads_secrets_only_over_ssh_stdin(tmp_path, monkeypatch):
    import base64

    from app.config import Settings

    deployment = cloud(tmp_path, FakeYC())
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("", encoding="utf-8")
    calls = []

    def ssh(instance, command, **kwargs):
        calls.append((command, kwargs))
        return "Готово"

    monkeypatch.setattr(deployment, "ssh", ssh)
    monkeypatch.setattr(deployment, "wait_ssh", Mock())

    settings = Settings(
        bot_token="secret-token",
        postgres_password="p$a#s's",
        telegram_proxy_url="http://user:password@proxy:3128",
        mcp_shared_secret="m" * 40,
    )
    deployment.deploy({}, settings)

    assert all("secret-token" not in command for command, _ in calls)

    env_payload = next(
        kw["input_text"] for cmd, kw in calls if "course-env.pending" in cmd and "base64" in cmd
    )
    from io import StringIO

    values = dotenv_values(
        stream=StringIO(base64.b64decode(env_payload).decode()), interpolate=False
    )
    assert values["BOT_TOKEN"] == "secret-token"
    assert values["POSTGRES_PASSWORD"] == "p$a#s's"
    assert values["POSTGRES_HOST"] == "db"
    assert values["MCP_SHARED_SECRET"] == "m" * 40
    assert all("m" * 40 not in command for command, _ in calls)
    deployment_command = next(command for command, _ in calls if "tar -xzf" in command)
    assert "config --services" in deployment_command
    assert "stop mcp-server" in deployment_command

    cloud_init = next(kwargs for cmd, kwargs in calls if "cloud-init status" in cmd)
    deployment_cmd = next(kwargs for cmd, kwargs in calls if "--wait --wait-timeout 180" in cmd)
    assert cloud_init["stream_output"] is True
    assert deployment_cmd["stream_output"] is True
    assert all(
        not kwargs.get("stream_output", False)
        for command, kwargs in calls
        if "base64 -d" in command
    )
    assert not (tmp_path / ".deploy" / "source.tar.gz").exists()


def test_cloud_help_renders(capsys):
    from scripts.cli import parser

    with pytest.raises(SystemExit) as exc:
        parser().parse_args(["cloud", "--help"])

    assert exc.value.code == 0
    assert "Гарантия vCPU, %" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ("status", "ps --all"),
        ("logs", "logs --tail 100 bot mcp-server db"),
    ],
)
def test_cloud_status_and_logs_include_mcp_server(tmp_path, monkeypatch, action, expected):
    import scripts.cloud as cloud_module
    from scripts.cli import parser

    cloud(tmp_path, FakeYC())
    commands = []
    monkeypatch.setattr(cloud_module.shutil, "which", lambda _: "/usr/bin/ssh")
    monkeypatch.setattr(cloud_module, "install_yc", lambda: "/usr/bin/yc")
    monkeypatch.setattr(cloud_module, "authenticate", lambda _: "folder-1")
    monkeypatch.setattr(cloud_module.Cloud, "instance", lambda _: {"id": "vm", "status": "RUNNING"})
    monkeypatch.setattr(
        cloud_module.Cloud,
        "ssh",
        lambda _self, _instance, command, **_kwargs: commands.append(command) or "ok",
    )

    result = cloud_module.cloud_main(tmp_path, parser().parse_args(["cloud", action]))

    assert result == 0
    assert commands == [f"{cloud_module.COMPOSE} {expected}"]


def test_setup_only_checks_real_credentials_without_starting_bot(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from scripts import local

    prepare_env(
        tmp_path,
        cloud=False,
        ask=Mock(side_effect=["123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk", ""]),
        environ={},
    )
    commands = Mock(return_value="")
    database = AsyncMock()
    launch = Mock()

    monkeypatch.setattr(local, "run_command", commands)
    monkeypatch.setattr(local, "check_database", database)
    monkeypatch.setattr(local.subprocess, "call", launch)

    result = local.local_main(tmp_path, "up", setup_only=True)

    assert result == 0
    assert "--wait" in commands.call_args.args[0]
    database.assert_awaited_once()
    launch.assert_not_called()


def test_custom_vm_configuration_is_remembered(tmp_path):
    from scripts.cli import parser

    fake = FakeYC()
    deployment = cloud(tmp_path, fake)
    key = tmp_path / ".deploy" / "id_ed25519"
    key.write_text("private-test", encoding="utf-8")
    key.with_suffix(".pub").write_text("ssh-ed25519 public-test", encoding="utf-8")

    first = parser().parse_args(["cloud", "--ssh-cidr", "203.0.113.10/32", "--memory", "2"])
    deployment.provision(first)
    deployment.provision(parser().parse_args(["cloud"]))

    assert deployment.state["config"]["memory"] == 2
    assert len(fake.created) == 4
