import json
from urllib.request import ProxyHandler, build_opener


def endpoint_is_healthy(host: str, port: int) -> bool:
    if not 1 <= port <= 65535:
        return False

    address = f"[{host}]" if ":" in host and not host.startswith("[") else host
    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open(f"http://{address}:{port}/health", timeout=5) as response:
            body = json.load(response)
    except (OSError, ValueError):
        return False

    return isinstance(body, dict) and body.get("status") == "ok"
