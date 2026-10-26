import json
import os
import urllib.request


def main() -> int:
    port = int(os.environ.get("MCP_PORT", "8000"))
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as response:
            return 0 if json.load(response).get("status") == "ok" else 1
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
