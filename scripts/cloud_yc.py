import json
import os
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from urllib.request import urlopen

from scripts.common import CommandError, run_command


def install_yc() -> str:
    found = shutil.which("yc")
    usual = Path.home() / "yandex-cloud" / "bin" / ("yc.exe" if os.name == "nt" else "yc")

    if found or usual.exists():
        return found or str(usual)

    print("Устанавливаем Yandex Cloud CLI официальным установщиком…", flush=True)
    extension = "ps1" if os.name == "nt" else "sh"

    with tempfile.TemporaryDirectory(prefix="itmo-yc-") as directory:
        installer = Path(directory) / f"install.{extension}"

        with urlopen(
            f"https://storage.yandexcloud.net/yandexcloud-yc/install.{extension}", timeout=60
        ) as response:
            installer.write_bytes(response.read())

        command = (
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"]
            if os.name == "nt"
            else ["bash"]
        )
        run_command(
            [*command, str(installer)],
            label="Установка yc",
            interactive=True,
            timeout=600,
        )

    if not usual.exists():
        raise CommandError("yc не найден после установки. Добавьте его каталог в PATH и повторите.")

    return str(usual)


def authenticate(yc_path: str) -> str:
    try:
        run_command([yc_path, "iam", "create-token"], label="Проверка авторизации yc")
    except CommandError:
        run_command(
            [yc_path, "init"],
            label="Вход в Yandex Cloud",
            interactive=True,
            timeout=1800,
        )

    folder = run_command(
        [yc_path, "config", "get", "folder-id"], label="Чтение каталога yc"
    ).strip()

    if not folder:
        raise CommandError("Выполните yc init и выберите каталог с подключённым биллингом.")

    return folder


def make_yc_client(yc_path: str, folder: str) -> Callable:
    def yc(command: list[str]):
        output = run_command(
            [yc_path, *command, "--folder-id", folder, "--format", "json"],
            label="Yandex Cloud: проверьте профиль, права, квоты и параметры ВМ",
            timeout=600,
            error_prefix="ERROR:",
        )
        return json.loads(output) if output.strip() else {}

    return yc
