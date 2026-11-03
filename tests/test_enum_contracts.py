import json
from enum import StrEnum

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from app.config import ContextProvider, MCPTransport, Settings
from app.core.schemas.agent import (
    AgentDecision,
    AgentResult,
    ClarificationArguments,
    TurnState,
)
from app.core.schemas.execution import StepAction
from app.core.schemas.llm import FinishReason, LLMResponse
from app.core.schemas.tools import WeatherResult, WeatherStatus
from app.db.records import ConfirmationState
from scripts.run_routing_eval import MeasurementStatus
from tests.mcp_catalog import tool_catalog


@pytest.mark.parametrize(
    "enum_type",
    [
        AgentDecision,
        StepAction,
        TurnState,
        ConfirmationState,
        WeatherStatus,
        FinishReason,
        MCPTransport,
        ContextProvider,
        MeasurementStatus,
    ],
)
def test_enum_values_remain_strings_for_json_and_environment(enum_type: type[StrEnum]) -> None:
    for member in enum_type:
        assert str(member) == member.value
        assert json.loads(json.dumps(member)) == member.value


@pytest.mark.parametrize("decision", list(AgentDecision))
def test_agent_result_accepts_existing_string_payloads(decision: AgentDecision) -> None:
    result = AgentResult.model_validate(
        {
            "text": "Ответ",
            "decision": decision.value,
            **(
                {"confirmation_id": "confirmation-123"} if decision == AgentDecision.PREPARE else {}
            ),
        }
    )
    assert result.decision is decision
    assert json.loads(result.model_dump_json())["decision"] == decision.value


def test_clarification_arguments_accept_public_question() -> None:
    result = ClarificationArguments.model_validate({"question": "  Какой день?  "})
    assert result.question == "Какой день?"


@pytest.mark.parametrize("payload", [{"question": "  "}, {"question": "Вопрос", "extra": True}])
def test_clarification_arguments_reject_empty_and_extra_fields(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ClarificationArguments.model_validate(payload)


@pytest.mark.parametrize("reason", list(FinishReason))
def test_llm_response_accepts_existing_finish_reasons(reason: FinishReason) -> None:
    response = LLMResponse.model_validate({"content": "Ответ", "finish_reason": reason.value})
    assert response.finish_reason is reason
    assert response.model_dump(mode="json")["finish_reason"] == reason.value


def test_weather_enum_keeps_discovered_output_schema_and_json_compatible() -> None:
    payload = {"status": "not_found"}
    weather = WeatherResult.model_validate(payload)
    assert weather.status is WeatherStatus.NOT_FOUND
    assert weather.model_dump(mode="json")["status"] == "not_found"
    schema = next(tool.output_schema for tool in tool_catalog() if tool.name == "get_weather")
    assert schema is not None
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    assert validator.is_valid(payload)
    assert not validator.is_valid({"status": "unknown"})


def test_configuration_loads_enum_instances_and_rejects_unknown_values(tmp_path) -> None:
    environment = {
        "BOT_TOKEN": "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk",
        "POSTGRES_PASSWORD": "unused",
        "MCP_TRANSPORT": "streamable-http",
        "LLM_CONTEXT_PROVIDER": "models",
    }
    settings = Settings.load(tmp_path / "missing.env", environ=environment)
    assert settings.mcp_transport is MCPTransport.STREAMABLE_HTTP
    assert settings.llm_context_provider is ContextProvider.MODELS
    for key in ("MCP_TRANSPORT", "LLM_CONTEXT_PROVIDER"):
        with pytest.raises(ValueError, match=key):
            Settings.load(tmp_path / "missing.env", environ={**environment, key: "unknown"})
