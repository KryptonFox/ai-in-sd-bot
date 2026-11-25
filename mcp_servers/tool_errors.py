import json
from typing import Never

from fastmcp.exceptions import ToolError


def raise_tool_error(code: str, message: str) -> Never:
    payload = {"error": {"code": code, "message": message}}
    raise ToolError(json.dumps(payload, ensure_ascii=False))
