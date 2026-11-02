import asyncio
import logging
import re
from urllib.parse import urlsplit

import httpx

from app.config import ContextProvider, Settings

logger = logging.getLogger("app.llm.context")
DEFAULT_CONTEXT_LENGTH = 32768


class ContextLengthResolver:
    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._client = client
        self._owns_client = client is None
        self._cache: dict[tuple[str, str], int] = {}

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    async def get(self, model: str) -> int:
        base_url = self._settings.llm_base_url.rstrip("/")
        key = (base_url, model)
        if key in self._cache:
            return self._cache[key]

        provider = self._provider(base_url)
        detected = None

        try:
            if provider != ContextProvider.NONE:
                if self._client is None:
                    self._client = httpx.AsyncClient(timeout=5.0)

                async with asyncio.timeout(5):
                    if provider == ContextProvider.OLLAMA:
                        detected = await self._ollama_length(
                            self._client,
                            base_url,
                            model,
                        )
                    else:
                        detected = await self._models_length(
                            self._client,
                            base_url,
                            model,
                        )
        except (httpx.HTTPError, TimeoutError, ValueError, TypeError, AttributeError):
            logger.debug("context_metadata_unavailable")

        if type(detected) is not int or detected <= 0:
            detected = self._settings.default_context_length

        self._cache[key] = detected

        return detected

    def _provider(self, base_url: str) -> ContextProvider:
        parsed = urlsplit(base_url)
        provider = self._settings.llm_context_provider
        if provider == ContextProvider.AUTO:
            if parsed.hostname == "openrouter.ai":
                provider = ContextProvider.MODELS
            elif parsed.port == 11434 or parsed.hostname == "ollama":
                provider = ContextProvider.OLLAMA
            else:
                provider = ContextProvider.NONE

        return provider

    async def _ollama_length(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        model: str,
    ) -> object:
        origin = base_url.removesuffix("/v1")
        response = await client.post(f"{origin}/api/show", json={"name": model})
        response.raise_for_status()
        data = response.json()

        for name, value in data.get("model_info", {}).items():
            if name.endswith(".context_length") and type(value) is int and value > 0:
                return value

        match = re.search(r"num_ctx\s+(\d+)", str(data.get("parameters", "")))
        if match:
            return int(match.group(1))

        return None

    async def _models_length(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        model: str,
    ) -> object:
        headers = {"Authorization": f"Bearer {self._settings.llm_api_key}"}
        response = await client.get(f"{base_url}/models", headers=headers)
        response.raise_for_status()

        for item in response.json().get("data", []):
            if item.get("id") == model:
                return item.get("context_length") or item.get("max_model_len")

        return None
