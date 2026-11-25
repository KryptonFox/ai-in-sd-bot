import argparse

from app.config import ConfigError, Settings
from app.core.health_probe import endpoint_is_healthy


def main() -> int:
    parser = argparse.ArgumentParser(description="Проверить приложение и подключение БД")
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()

    try:
        settings = Settings.load(args.env_file)
    except ConfigError as exc:
        print(str(exc))
        return 1

    if not endpoint_is_healthy("127.0.0.1", settings.health_port):
        print("Приложение не готово: проверьте запуск бота, подключение БД и логи.")
        return 1

    print("Приложение готово: polling работает, PostgreSQL отвечает на SELECT 1.")
    print("Доступность Telegram при сетевых сбоях проверяйте по логам бота.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
