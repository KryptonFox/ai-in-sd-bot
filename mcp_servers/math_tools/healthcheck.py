import argparse
import json
import os
import urllib.request

from dotenv import dotenv_values


def main() -> int:
    parser = argparse.ArgumentParser(description="Health check for the math MCP server")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--env-file")
    args = parser.parse_args()

    file_values = dotenv_values(args.env_file) if args.env_file else {}
    host = args.host or os.environ.get("MCP_HEALTH_HOST") or file_values.get("MCP_HEALTH_HOST")
    port = (
        args.port
        if args.port is not None
        else _port(os.environ.get("MCP_PORT") or file_values.get("MCP_PORT"))
    )
    if not host:
        host = "127.0.0.1"
    if port is None or not 1 <= port <= 65535:
        return 1

    try:
        with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=5) as response:
            return 0 if json.load(response).get("status") == "ok" else 1
    except Exception:
        return 1


def _port(value: object) -> int | None:
    if value is None:
        return 8000
    try:
        return int(str(value))
    except ValueError:
        return None


if __name__ == "__main__":
    raise SystemExit(main())
