import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    uv = shutil.which("uv")
    if uv is None:
        print("Нужен uv. Установите его по инструкции в README.")
        return 1

    env = {**os.environ, "PYTHONUTF8": "1"}

    try:
        subprocess.run([uv, "sync", "--locked"], cwd=ROOT, env=env, check=True)
        return subprocess.call(
            [uv, "run", "--locked", "python", "-m", "scripts.cli", *sys.argv[1:]],
            cwd=ROOT,
            env=env,
        )
    except (OSError, subprocess.CalledProcessError):
        print("Не удалось подготовить окружение. Проверьте uv, доступ к PyPI и вывод выше.")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
