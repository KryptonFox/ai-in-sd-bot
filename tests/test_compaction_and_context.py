from unittest.mock import AsyncMock, MagicMock

from app.config import Settings
from app.llm.client import DEFAULT_CONTEXT_LENGTH, OpenAIClient


def test_default_context_length_value():
    assert DEFAULT_CONTEXT_LENGTH == 32768


async def test_context_length_fallback_to_32k():
    settings = Settings(
        bot_token="123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk",
        postgres_password="unused",
        llm_api_key="key",
        llm_base_url="http://invalid.local/v1",
        llm_model="test-model",
    )
    client = OpenAIClient(settings)
    ctx_len = await client.get_context_length()
    assert ctx_len == 32768


async def test_context_length_autodetect_ollama():
    mock_http = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "model_info": {
            "general.architecture": "qwen2",
            "qwen2.context_length": 40960,
        }
    }
    mock_http.post = AsyncMock(return_value=mock_resp)

    settings = Settings(
        bot_token="123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk",
        postgres_password="unused",
        llm_api_key="key",
        llm_base_url="http://127.0.0.1:11434/v1",
        llm_model="qwen3:14b",
    )
    client = OpenAIClient(settings, client=mock_http)
    ctx_len = await client.get_context_length("qwen3:14b")
    assert ctx_len == 40960

    mock_http.post.assert_awaited_once()
    call_args = mock_http.post.call_args
    assert call_args[0][0] == "http://127.0.0.1:11434/api/show"
    assert call_args[1]["json"] == {"name": "qwen3:14b"}


async def test_context_length_autodetect_openrouter():
    mock_http = MagicMock()
    mock_resp_ollama = MagicMock()
    mock_resp_ollama.status_code = 404

    mock_resp_models = MagicMock()
    mock_resp_models.status_code = 200
    mock_resp_models.json.return_value = {
        "data": [
            {"id": "openai/gpt-4o-mini", "context_length": 128000},
            {"id": "anthropic/claude-3-haiku", "context_length": 200000},
        ]
    }
    mock_http.post = AsyncMock(return_value=mock_resp_ollama)
    mock_http.get = AsyncMock(return_value=mock_resp_models)

    settings = Settings(
        bot_token="123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk",
        postgres_password="unused",
        llm_api_key="sk-or-test",
        llm_base_url="https://openrouter.ai/api/v1",
        llm_model="openai/gpt-4o-mini",
    )
    client = OpenAIClient(settings, client=mock_http)
    ctx_len = await client.get_context_length("openai/gpt-4o-mini")
    assert ctx_len == 128000


def test_settings_compaction_defaults():
    settings = Settings(
        bot_token="123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk",
        postgres_password="unused",
    )
    assert settings.default_context_length == 32768
    assert settings.compaction_threshold_messages == 10
    assert settings.compaction_keep_recent == 3
