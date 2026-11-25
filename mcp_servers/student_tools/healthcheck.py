import os

from app.core.health_probe import endpoint_is_healthy


def main() -> int:
    try:
        port = int(os.environ.get("MCP_PORT", "8000"))
    except ValueError:
        return 1
    return 0 if endpoint_is_healthy("127.0.0.1", port) else 1


if __name__ == "__main__":
    raise SystemExit(main())
