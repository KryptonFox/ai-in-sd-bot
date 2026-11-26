import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.core.exceptions import LLMErrorCode, LLMServiceError
from app.core.schemas.message import ChatMessage
from app.core.schemas.routing import RouteDecision, RouteTarget
from app.core.trace import OperationTrace, trace_step
from scripts import run_lab3_routing_eval as evaluator

OFFICIAL_EXPECTED = {
    "schedule_1": RouteTarget.SCHEDULE,
    "schedule_2": RouteTarget.SCHEDULE,
    "schedule_3": RouteTarget.SCHEDULE,
    "schedule_4": RouteTarget.SCHEDULE,
    "knowledge_1": RouteTarget.KNOWLEDGE,
    "knowledge_2": RouteTarget.KNOWLEDGE,
    "knowledge_3": RouteTarget.KNOWLEDGE,
    "knowledge_4": RouteTarget.KNOWLEDGE,
    "lifestyle_1": RouteTarget.LIFESTYLE,
    "lifestyle_2": RouteTarget.LIFESTYLE,
    "lifestyle_3": RouteTarget.LIFESTYLE,
    "lifestyle_4": RouteTarget.LIFESTYLE,
    "clarify_1": RouteTarget.CLARIFY,
    "clarify_2": RouteTarget.CLARIFY,
    "clarify_3": RouteTarget.CLARIFY,
}


@pytest.fixture(autouse=True)
def stable_metadata(monkeypatch):
    monkeypatch.setattr(evaluator, "_app_python_sha256", lambda: "test-app")
    monkeypatch.setattr(evaluator, "_revision", lambda: "test-revision")
    monkeypatch.setattr(evaluator, "_dirty", lambda: False)


def mock_config(
    output_dir: Path,
    *,
    mock_scenario: evaluator.MockScenario = evaluator.MockScenario.SUCCESS,
    case_ids: frozenset[str] = frozenset(),
) -> evaluator.EvaluationConfig:
    return evaluator.EvaluationConfig(
        mode=evaluator.RunMode.MOCK,
        mock_scenario=mock_scenario,
        output_dir=output_dir,
        label="test-mock",
        case_ids=case_ids,
    )


async def run_mock(
    output_dir: Path,
    *,
    mock_scenario: evaluator.MockScenario = evaluator.MockScenario.SUCCESS,
    case_ids: frozenset[str] = frozenset(),
) -> tuple[evaluator.EvaluationReport, Path]:
    return await evaluator.run_evaluation(
        mock_config(output_dir, mock_scenario=mock_scenario, case_ids=case_ids)
    )


def test_official_dataset_keeps_the_original_15_cases():
    dataset, snapshot = evaluator.load_official_dataset()

    assert dataset.version == 1
    assert len(dataset.cases) == 15
    assert snapshot["case_count"] == 15
    assert snapshot["sha256"] == (
        "3a1e058d8f5c467eb84ac775f886b148b14a74c326015ebda9d175e26fb39236"
    )
    assert {case.id: case.expected_target for case in dataset.cases} == OFFICIAL_EXPECTED


def test_supplemental_dataset_separates_math_and_web_cases():
    dataset, snapshot = evaluator.load_supplemental_dataset()
    official, _ = evaluator.load_official_dataset()

    math_cases = [case for case in dataset.cases if case.suite is evaluator.SupplementalSuite.MATH]
    web_cases = [case for case in dataset.cases if case.suite is evaluator.SupplementalSuite.WEB]

    assert dataset.version == 1
    assert snapshot["case_count"] == len(dataset.cases)
    assert len(math_cases) == 8
    assert len(web_cases) == 7
    assert all(case.expected_target is RouteTarget.MATH for case in math_cases)
    assert all(case.expected_target is RouteTarget.KNOWLEDGE for case in web_cases)
    assert any(case.boundary for case in math_cases)
    assert any(case.boundary for case in web_cases)
    assert any(case.history for case in math_cases)
    assert any(case.history for case in web_cases)

    official_ids = {case.id for case in official.cases}
    supplemental_ids = {case.id for case in dataset.cases}
    assert official_ids.isdisjoint(supplemental_ids)
    assert len(supplemental_ids) == len(dataset.cases)


async def test_mock_run_scores_suites_and_writes_human_readable_json(tmp_path: Path):
    report, path = await run_mock(tmp_path)

    assert report["summary"]["official"]["total"] == 15
    assert report["summary"]["official"]["correct"] == 15
    assert report["summary"]["math"]["total"] == 8
    assert report["summary"]["math"]["correct"] == 8
    assert report["summary"]["web"]["total"] == 7
    assert report["summary"]["web"]["correct"] == 7
    assert report["summary"]["overall"]["correct"] == report["summary"]["overall"]["total"] == 30
    assert report["summary"]["overall"]["suite"] == "overall"
    assert report["verification"]["checks_passed"]
    assert report["verification"]["official_denominator"] == 15
    assert report["verification"]["supplemental_denominators"] == {"math": 8, "web": 7}
    assert report["router"]["provider"] == evaluator.MOCK_PROVIDER
    assert report["router"]["api_key_configured"] is False
    assert report["mode"] == "mock"
    assert all(case["usage"] is None for case in report["cases"])
    assert all(case["allowance"] is None for case in report["cases"])

    text = path.read_text(encoding="utf-8")
    assert "Какие пары у меня завтра?" in text
    assert "Санкт-Петербург" in text
    assert "\\u041a" not in text
    assert '\n  "schema_version": 1' in text
    parsed = json.loads(text)
    assert parsed["summary"]["official"]["total"] == 15


async def test_mock_faults_stay_in_the_denominator(tmp_path: Path):
    report, _ = await run_mock(
        tmp_path,
        mock_scenario=evaluator.MockScenario.FAULTS,
    )

    official = report["summary"]["official"]
    math = report["summary"]["math"]
    web = report["summary"]["web"]

    assert (official["total"], official["correct"], official["errors"], official["wrong"]) == (
        15,
        13,
        1,
        1,
    )
    assert (math["total"], math["correct"], math["errors"], math["wrong"]) == (8, 7, 1, 0)
    assert (web["total"], web["correct"], web["errors"], web["wrong"]) == (7, 6, 0, 1)
    assert report["summary"]["overall"]["total"] == 30
    assert report["summary"]["overall"]["errors"] == 2
    assert report["summary"]["overall"]["wrong"] == 2
    assert report["summary"]["overall"]["error_counts"] == {
        "llm_rate_limit": 1,
        "llm_timeout": 1,
    }
    assert report["verification"]["failures_in_denominator"]
    assert report["verification"]["checks_passed"]
    assert official["total"] == official["correct"] + official["wrong"] + official["errors"]


async def test_case_filter_changes_the_selected_denominator(tmp_path: Path):
    report, _ = await run_mock(
        tmp_path,
        case_ids=frozenset({"schedule_2", "math_1"}),
    )

    assert report["selection"]["filtered_by_case_ids"]
    assert report["selection"]["official_complete"] is False
    assert report["summary"]["official"]["total"] == 1
    assert report["summary"]["math"]["total"] == 1
    assert report["summary"]["web"]["total"] == 0
    assert report["summary"]["overall"]["total"] == 2
    assert report["verification"]["official_denominator"] == 1
    assert report["verification"]["checks_passed"]


async def test_unknown_case_id_is_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="unknown case ids"):
        await run_mock(tmp_path, case_ids=frozenset({"missing_case"}))


def test_typed_config_rejects_unknown_fields(tmp_path: Path):
    config = evaluator.EvaluationConfig(
        mode=evaluator.RunMode.MOCK,
        output_dir=tmp_path,
        label="valid",
    )
    assert config.mode is evaluator.RunMode.MOCK

    with pytest.raises(ValidationError):
        evaluator.EvaluationConfig.model_validate(
            {
                "mode": "mock",
                "output_dir": str(tmp_path),
                "label": "invalid",
                "unexpected": True,
            }
        )


def test_error_details_do_not_include_provider_messages():
    secret = "sk-secret-provider-response"
    error = evaluator._case_error(
        LLMServiceError(
            secret,
            code=LLMErrorCode.AUTHENTICATION,
            status_code=401,
        )
    )

    assert error["code"] == "llm_authentication"
    assert error["status_code"] == 401
    assert secret not in json.dumps(error)
    assert "message" not in error


class _UsageRouter:
    async def route(self, text: str, history: Sequence[ChatMessage]) -> RouteDecision:
        with trace_step("router", "api_request") as step:
            assert step is not None
            step.input_tokens = 10
            step.output_tokens = 2
        return RouteDecision(
            target=RouteTarget.MATH,
            reason="test_usage",
            confidence=0.9,
        )

    async def aclose(self) -> None:
        return None


async def test_live_provider_reads_usage_from_nested_trace():
    provider = evaluator.LiveRouteProvider(
        Settings(bot_token="", postgres_password=""),
        router=_UsageRouter(),
    )
    case = evaluator.EvaluationCase(
        id="usage_case",
        query="Проверь вычисление",
        expected_target=RouteTarget.MATH,
    )

    observation = await provider.route(case)
    await provider.aclose()

    assert observation.decision.target is RouteTarget.MATH
    assert observation.usage == {
        "input_tokens": 10,
        "output_tokens": 2,
        "total_tokens": 12,
    }


def test_trace_usage_is_null_when_the_provider_supplies_no_counts():
    trace = OperationTrace(
        user_id=0,
        request_id="no-usage",
        started_at=datetime(2026, 10, 12, 9, 0, tzinfo=UTC),
    )

    assert evaluator._trace_usage(trace) is None
