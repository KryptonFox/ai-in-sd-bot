from typing import cast
from unittest.mock import AsyncMock

from fastmcp import Client


def mock_sdk_client() -> tuple[Client, AsyncMock]:
    sdk = AsyncMock(spec=Client)
    sdk.__aenter__.return_value = sdk
    sdk.__aexit__.return_value = False
    sdk.close = AsyncMock()
    return cast(Client, sdk), sdk
