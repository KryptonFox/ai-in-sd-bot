import argparse
import ipaddress
import json
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import NotRequired, TypedDict

from app.config import Settings
from scripts import cloud_ssh
from scripts.cloud_ssh import COMPOSE
from scripts.common import CommandError, private_write, run_command

KINDS = {
    "network": ["vpc", "network"],
    "subnet": ["vpc", "subnet"],
    "security-group": ["vpc", "security-group"],
    "vm": ["compute", "instance"],
}


class ResourceReference(TypedDict, total=False):
    id: str
    name: str
    managed: bool


class CloudConfig(TypedDict):
    zone: str
    platform: str
    cores: int
    core_fraction: int
    memory: int
    disk_size: int


class CloudState(TypedDict):
    version: int
    folder_id: str
    owner: str
    resources: dict[str, ResourceReference]
    config: NotRequired[CloudConfig]
    ssh_cidr: NotRequired[str]


class Cloud:
    def __init__(self, root: Path, *, folder_id: str, yc: Callable):
        self.root = root
        self.yc = yc
        self.state: CloudState
        self.directory = root / ".deploy"
        self.state_path = self.directory / "state.json"
        self.directory.mkdir(exist_ok=True)

        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if self.state["folder_id"] != folder_id:
                raise CommandError("Состояние деплоя относится к другому каталогу Yandex Cloud.")
        else:
            self.state = {
                "version": 1,
                "folder_id": folder_id,
                "owner": secrets.token_hex(8),
                "resources": {},
            }
            self.save()

    def save(self) -> None:
        temporary = self.state_path.with_suffix(".tmp")
        private_write(
            temporary,
            json.dumps(
                self.state,
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )
        temporary.replace(self.state_path)

    def verify(self, resource: dict) -> None:
        if (
            resource.get("labels", {}).get("template-id") != self.state["owner"]
            or resource.get("folder_id") != self.state["folder_id"]
        ):
            raise CommandError("Ресурс не принадлежит этому проекту; операция отменена.")

    def lookup(self, key: str, kind: list[str]) -> dict | None:
        record = self.state["resources"].get(key)
        if not record:
            return None

        resources = self.yc([*kind, "list"])
        matches = []
        resource_id = record.get("id")

        for resource in resources:
            if resource_id:
                matches_record = resource["id"] == resource_id
            else:
                matches_record = resource["name"] == record["name"]

            if matches_record:
                matches.append(resource)

        if len(matches) > 1:
            raise CommandError("Найдено несколько одноимённых ресурсов; проверьте консоль облака.")

        if matches:
            if record.get("managed", True):
                self.verify(matches[0])
            elif matches[0].get("folder_id") != self.state["folder_id"]:
                raise CommandError("Внешний ресурс относится к другому каталогу Yandex Cloud.")

            return matches[0]

        return None

    def ensure(
        self,
        key: str,
        kind: list[str],
        flags: list[str],
    ) -> dict:
        name = f"itmo-{self.state['owner']}-{key}"
        self.state["resources"].setdefault(key, {"name": name})
        self.save()

        resource = self.lookup(key, kind)
        if resource is None:
            if self.state["resources"][key].get("id"):
                raise CommandError(
                    f"Ранее созданный {key} исчез. Проверьте состояние облака перед новым деплоем."
                )

            print(f"Создаём ресурс: {key}…", flush=True)
            resource = self.yc(
                [
                    *kind,
                    "create",
                    "--name",
                    name,
                    "--labels",
                    f"template-id={self.state['owner']}",
                    *flags,
                ]
            )
            self.verify(resource)

        self.state["resources"][key]["id"] = resource["id"]
        self.save()

        return resource

    def ensure_network(self) -> dict:
        resource = self.lookup("network", KINDS["network"])
        if resource is not None:
            return resource

        record = self.state["resources"].get("network")
        if record and record.get("id"):
            raise CommandError("Ранее использованная сеть исчезла. Проверьте состояние облака.")

        defaults = [
            network
            for network in self.yc([*KINDS["network"], "list"])
            if network.get("name") == "default"
        ]

        if len(defaults) > 1:
            raise CommandError("Найдено несколько сетей default; проверьте консоль облака.")

        if defaults:
            resource = defaults[0]
            if resource.get("folder_id") != self.state["folder_id"]:
                raise CommandError("Сеть default относится к другому каталогу Yandex Cloud.")

            self.state["resources"]["network"] = {
                "id": resource["id"],
                "name": resource["name"],
                "managed": False,
            }
            self.save()
            print("Используем существующую сеть default.", flush=True)

            return resource

        return self.ensure("network", KINDS["network"], [])

    def destroy(self, *, confirmed: bool) -> None:
        if not confirmed:
            raise CommandError("Необходимо явное подтверждение удаления БД и ресурсов.")

        found = {key: self.lookup(key, kind) for key, kind in KINDS.items()}

        for key in reversed(KINDS):
            resource = found[key]
            record = self.state["resources"].get(key, {})
            if resource and record.get("managed", True):
                self.yc([*KINDS[key], "delete", "--id", resource["id"]])

            self.state["resources"].pop(key, None)
            self.save()

        self.state_path.unlink()
        print("Ресурсы проекта удалены. Облачная БД и загрузочный диск удалены вместе с ВМ.")

    def key(self) -> Path:
        key = self.directory / "id_ed25519"

        if not key.exists():
            if "vm" in self.state["resources"]:
                raise CommandError("Утерян SSH-ключ существующей ВМ. Восстановите его из копии.")

            run_command(
                [
                    "ssh-keygen",
                    "-t",
                    "ed25519",
                    "-N",
                    "",
                    "-f",
                    str(key),
                    "-C",
                    f"itmo-{self.state['owner']}",
                ],
                label="Создание SSH-ключа",
            )

        if not key.with_suffix(".pub").exists():
            raise CommandError("Нет публичной части ключа .deploy/id_ed25519.pub.")

        return key

    def _provision_config(
        self,
        args: argparse.Namespace,
    ) -> tuple[CloudConfig, str]:
        defaults: CloudConfig = {
            "zone": "ru-central1-a",
            "platform": "standard-v2",
            "cores": 2,
            "core_fraction": 5,
            "memory": 1,
            "disk_size": 20,
        }
        config: CloudConfig = {**defaults, **self.state.get("config", {})}
        if args.zone is not None:
            config["zone"] = args.zone

        if args.platform is not None:
            config["platform"] = args.platform

        if args.cores is not None:
            config["cores"] = args.cores

        if args.core_fraction is not None:
            config["core_fraction"] = args.core_fraction

        if args.memory is not None:
            config["memory"] = args.memory

        if args.disk_size is not None:
            config["disk_size"] = args.disk_size

        if "config" in self.state and config != self.state["config"]:
            raise CommandError(
                "Параметры существующей ВМ отличаются. Используйте прежние аргументы."
            )

        cidr = args.ssh_cidr or self.state.get("ssh_cidr")
        if not cidr:
            raise CommandError("Укажите --ssh-cidr ВАШ_ПУБЛИЧНЫЙ_IP/32 для доступа по SSH.")

        try:
            network = ipaddress.ip_network(cidr, strict=True)
            if network.version != 4 or network.prefixlen == 0:
                raise ValueError
        except ValueError:
            raise CommandError("--ssh-cidr: нужен IPv4/CIDR, например 203.0.113.10/32.") from None

        if self.state.get("ssh_cidr") and cidr != self.state["ssh_cidr"]:
            raise CommandError(
                "IP изменился: сначала выполните deploy access --ssh-cidr НОВЫЙ_IP/32."
            )

        self.state.update(config=config, ssh_cidr=cidr)
        self.save()

        return config, cidr

    def provision(self, args: argparse.Namespace) -> dict:
        config, cidr = self._provision_config(args)
        key = self.key()
        network = self.ensure_network()

        subnet = self.ensure(
            "subnet",
            KINDS["subnet"],
            ["--network-id", network["id"], "--zone", config["zone"], "--range", "10.120.0.0/24"],
        )
        security = self.ensure(
            "security-group",
            KINDS["security-group"],
            [
                "--network-id",
                network["id"],
                "--rule",
                f"direction=ingress,protocol=tcp,port=22,v4-cidrs={cidr}",
                "--rule",
                "direction=egress,protocol=any,port=any,v4-cidrs=0.0.0.0/0",
            ],
        )

        cloud_init = self._write_cloud_init(key)

        return self.ensure(
            "vm",
            KINDS["vm"],
            [
                "--zone",
                config["zone"],
                "--platform",
                config["platform"],
                "--cores",
                str(config["cores"]),
                "--core-fraction",
                str(config["core_fraction"]),
                "--memory",
                str(config["memory"]),
                "--create-boot-disk",
                f"image-folder-id=standard-images,image-family=ubuntu-2404-lts,"
                f"type=network-hdd,size={config['disk_size']},auto-delete=true",
                "--network-interface",
                f"subnet-id={subnet['id']},nat-ip-version=ipv4,security-group-ids={security['id']}",
                "--metadata-from-file",
                f"user-data={cloud_init}",
            ],
        )

    def _write_cloud_init(self, key: Path) -> Path:
        cloud_init = self.directory / "cloud-init.yaml"
        initialization = {
            "groups": ["docker"],
            "users": [
                {
                    "name": "student",
                    "groups": ["sudo", "docker"],
                    "shell": "/bin/bash",
                    "sudo": "ALL=(ALL) NOPASSWD:ALL",
                    "lock_passwd": True,
                    "ssh_authorized_keys": [key.with_suffix(".pub").read_text().strip()],
                }
            ],
            "ssh_pwauth": False,
            "disable_root": True,
            "package_update": True,
            "packages": ["docker.io", "docker-compose-v2"],
            "runcmd": [
                [
                    "bash",
                    "-ceu",
                    (
                        "systemctl enable --now docker\n"
                        "if [ ! -f /swapfile ]; then\n"
                        "  fallocate -l 1G /swapfile\n"
                        "  chmod 600 /swapfile\n"
                        "  mkswap /swapfile\n"
                        "  swapon /swapfile\n"
                        "  echo '/swapfile none swap sw 0 0' >> /etc/fstab\n"
                        "fi\n"
                    ),
                ]
            ],
        }

        private_write(cloud_init, "#cloud-config\n" + json.dumps(initialization, indent=2) + "\n")

        return cloud_init

    def instance(self) -> dict:
        instance = self.lookup("vm", KINDS["vm"])
        if not instance:
            raise CommandError("ВМ проекта не найдена. Сначала выполните deploy up.")

        return self.yc(["compute", "instance", "get", "--id", instance["id"]])

    def ssh(
        self,
        instance: dict,
        command: str,
        *,
        timeout: int = 120,
        input_text: str | None = None,
        stream_output: bool = False,
    ) -> str:
        return cloud_ssh.ssh(
            self.directory,
            instance,
            command,
            timeout=timeout,
            input_text=input_text,
            stream_output=stream_output,
        )

    def wait_ssh(self, instance: dict) -> None:
        cloud_ssh.wait_for_ssh(self.ssh, instance)

    def deploy(self, instance: dict, settings: Settings) -> None:
        self.wait_ssh(instance)
        cloud_ssh.deploy(self.root, self.ssh, instance, settings)

    def access(self, cidr: str) -> None:
        try:
            parsed = ipaddress.ip_network(cidr, strict=True)
            if parsed.version != 4 or parsed.prefixlen == 0:
                raise ValueError
        except (ValueError, TypeError):
            raise CommandError("Для access укажите --ssh-cidr ВАШ_ПУБЛИЧНЫЙ_IP/32.") from None

        group = self.lookup("security-group", KINDS["security-group"])
        if not group:
            raise CommandError("Группа безопасности проекта не найдена.")

        full = self.yc(["vpc", "security-group", "get", "--id", group["id"]])
        flags = []

        for rule in full.get("rules", []):
            if rule.get("direction") == "INGRESS":
                flags.extend(["--delete-rule-id", rule["id"]])

        self.yc(
            [
                "vpc",
                "security-group",
                "update-rules",
                "--id",
                group["id"],
                *flags,
                "--add-rule",
                f"direction=ingress,protocol=tcp,port=22,v4-cidrs={cidr}",
            ]
        )
        self.state["ssh_cidr"] = cidr
        self.save()
        print("Доступ по SSH обновлён.")

    def set_instance_state(self, action: str) -> None:
        instance = self.instance()
        desired = "RUNNING" if action == "start" else "STOPPED"
        if instance.get("status") != desired:
            self.yc(["compute", "instance", action, "--id", instance["id"]])

        if action == "start":
            instance = self.instance()
            self.wait_ssh(instance)
            print(
                self.ssh(
                    instance,
                    f"{COMPOSE} up -d --wait --wait-timeout 180",
                    timeout=240,
                )
            )

        print(
            "ВМ запущена."
            if action == "start"
            else "ВМ остановлена. Диск продолжает тарифицироваться."
        )

    def show_status(self, action: str) -> None:
        instance = self.instance()
        print(f"ВМ: {instance['id']}; состояние: {instance.get('status', 'UNKNOWN')}")
        interfaces = instance.get("network_interfaces", [])
        if interfaces:
            address = interfaces[0].get("primary_v4_address", {}).get("one_to_one_nat", {})
            if address.get("address"):
                print(f"Публичный IPv4: {address['address']}")

        if instance.get("status") == "RUNNING":
            command = (
                "ps --all"
                if action == "status"
                else "logs --tail 100 bot student-tools-mcp math-tools-mcp db"
            )
            output = self.ssh(instance, f"{COMPOSE} {command}")
            print(output)
