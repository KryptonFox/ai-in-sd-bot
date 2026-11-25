import argparse
import os

from dotenv import dotenv_values

from app.core.health_probe import endpoint_is_healthy


def main() -> int:
    parser = argparse.ArgumentParser(description="Health check for the math MCP server")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--env-file")
    args = parser.parse_args()

    file_values = dotenv_values(args.env_file, interpolate=False) if args.env_file else {}
    host = args.host or os.environ.get("MCP_HEALTH_HOST") or file_values.get("MCP_HEALTH_HOST")
    port = (
        args.port
        if args.port is not None
        else _port(os.environ.get("MCP_PORT") or file_values.get("MCP_PORT"))
    )
    if not host:
        host = "127.0.0.1"
    return 0 if port is not None and endpoint_is_healthy(host, port) else 1


def _port(value: object) -> int | None:
    if value is None:
        return 8000
    try:
        return int(str(value))
    except ValueError:
        return None


if __name__ == "__main__":
    raise SystemExit(main())
