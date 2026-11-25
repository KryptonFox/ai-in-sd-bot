import argparse
import shutil
from pathlib import Path

from app.config import Settings
from scripts.cloud_resources import Cloud
from scripts.cloud_yc import authenticate, install_yc, make_yc_client
from scripts.common import CommandError
from scripts.local import prepare_env


def _destroy_confirmed(deployment: Cloud) -> None:
    name = f"itmo-{deployment.state['owner']}"
    network = deployment.state["resources"].get("network", {})
    if network.get("managed", True):
        print(f"Будут удалены ВМ, диск с БД и сеть проекта {name}.")
    else:
        print(
            "Будут удалены ВМ, диск с БД, подсеть и группа безопасности "
            "проекта. Общая сеть default останется."
        )

    confirmed = input(f"Для подтверждения введите {name}: ").strip() == name
    deployment.destroy(confirmed=confirmed)


def _execute_action(
    deployment: Cloud,
    args: argparse.Namespace,
    settings: Settings | None,
) -> None:
    if args.action == "destroy":
        _destroy_confirmed(deployment)
    elif args.action == "access":
        deployment.access(args.ssh_cidr)
    elif args.action == "up":
        assert settings is not None
        print("Перед деплоем остановите локальный бот с этим же токеном.", flush=True)
        instance = deployment.provision(args)
        if instance.get("status") == "STOPPED":
            deployment.yc(["compute", "instance", "start", "--id", instance["id"]])

        deployment.deploy(deployment.instance(), settings)
    elif args.action in {"start", "stop"}:
        deployment.set_instance_state(args.action)
    else:
        deployment.show_status(args.action)


def cloud_main(root: Path, args: argparse.Namespace) -> int:
    for command in ["ssh", "ssh-keygen"]:
        if not shutil.which(command):
            raise CommandError(f"Установите OpenSSH: не найдена команда {command}.")

    settings = None
    if args.action == "up":
        settings = Settings.load(prepare_env(root, cloud=True))

    if args.action != "up" and not (root / ".deploy" / "state.json").exists():
        raise CommandError("Нет состояния деплоя. Сначала выполните deploy up.")

    yc_path = install_yc()
    folder = authenticate(yc_path)
    folder = args.folder_id or folder

    deployment = Cloud(root, folder_id=folder, yc=make_yc_client(yc_path, folder))

    try:
        _execute_action(deployment, args, settings)
    except BaseException:
        print(
            "Операция не завершена. Состояние сохранено в .deploy/state.json; "
            "ресурсы могут тарифицироваться. Повторите команду либо выполните deploy destroy."
        )
        raise

    return 0
