import json

import pytest

import scripts.run_routing_eval as routing_eval
from scripts.run_routing_eval import (
    ExpectedDecision,
    MeasurementStatus,
    ScoredDecision,
    _app_python_sha256,
    _markdown_report,
    normalize_arguments,
    normalize_value,
    score_case,
    summarize_cases,
)


def test_route_error_also_counts_as_argument_error():
    case: ExpectedDecision = {
        "expected_action": "get_weather",
        "expected_arguments": {"city": "Санкт-Петербург"},
    }

    score = score_case(case, "respond", {})

    assert score["route_correct"] is False
    assert score["arguments_correct"] is False
    assert score["success"] is False


def test_argument_normalization_uses_trim_casefold_and_instant():
    actual = normalize_arguments(
        {
            "city": "  Санкт-Петербург ",
            "text": "  Проверить отчёт ",
            "remind_at": "2026-10-13T15:30:00Z",
        }
    )
    expected = normalize_arguments(
        {
            "city": "санкт-петербург",
            "text": "проверить отчёт",
            "remind_at": "2026-10-13T18:30:00+03:00",
        }
    )

    assert actual == expected
    assert normalize_value("city", " St. Petersburg ") == "st. petersburg"


def test_weather_default_date_accepts_null_or_today_but_rejects_tomorrow():
    case: ExpectedDecision = {
        "expected_action": "get_weather",
        "expected_arguments": {"city": "Казань"},
    }
    for arguments in (
        {"city": "Казань"},
        {"city": "Казань", "date": None},
        {"city": "Казань", "date": "2026-10-12"},
    ):
        assert score_case(case, "get_weather", arguments, today="2026-10-12")["success"]
    assert not score_case(
        case,
        "get_weather",
        {"city": "Казань", "date": "2026-10-13"},
        today="2026-10-12",
    )["success"]


def test_summary_uses_full_routing_and_tool_argument_denominators():
    cases: list[ScoredDecision] = [
        {"route_correct": True, "arguments_applicable": True, "arguments_correct": True},
        {"route_correct": False, "arguments_applicable": True, "arguments_correct": False},
        {"route_correct": True, "arguments_applicable": False, "arguments_correct": None},
    ]

    assert summarize_cases(cases) == {
        "routing_correct": 2,
        "routing_total": 3,
        "routing_accuracy": 2 / 3,
        "arguments_correct": 1,
        "arguments_total": 2,
        "arguments_accuracy": 0.5,
    }


def test_unavailable_markdown_says_zero_scores_do_not_measure_model_quality():
    report = _markdown_report(
        {
            "label": "baseline",
            "measurement_status": MeasurementStatus.UNAVAILABLE,
            "counts": {
                "planned": 12,
                "attempted": 2,
                "decisions_received": 0,
                "not_run": 10,
            },
            "summary": {
                "routing_correct": 0,
                "routing_total": 12,
                "routing_accuracy": 0.0,
                "arguments_correct": 0,
                "arguments_total": 9,
                "arguments_accuracy": 0.0,
            },
            "cases": [],
        }
    )

    assert "Measurement status: **unavailable**" in report
    assert "Decisions received: 0/12" in report
    assert "Cases attempted: 2/12" in report
    assert "Cases not run: 10" in report
    assert "not measured model quality" in report


def test_app_code_fingerprint_changes_when_python_source_changes(tmp_path, monkeypatch):
    app_root = tmp_path / "app"
    source = app_root / "module.py"
    source.parent.mkdir()
    source.write_text("value = 1\n", encoding="utf-8")
    monkeypatch.setattr(routing_eval, "ROOT", tmp_path)

    first = _app_python_sha256()
    source.write_text("value = 2\n", encoding="utf-8")
    second = _app_python_sha256()

    assert first != second
    assert len(first) == len(second) == 64


@pytest.mark.parametrize("action", ["get_weather", "get_schedule"])
def test_omitted_today_is_equivalent_to_explicit_today(action: str):
    case: ExpectedDecision = {
        "expected_action": action,
        "expected_arguments": {"date": "2026-10-12"},
    }

    assert score_case(case, action, {}, today="2026-10-12")["success"]


@pytest.mark.parametrize(
    ("action", "expected", "actual"),
    [
        ("get_weather", {"city": "Казань"}, {}),
        ("get_weather", {"date": "2026-10-13"}, {}),
        ("get_schedule", {"date": "2026-10-13"}, {}),
        ("get_weather", {}, {"city": "Санкт-Петербург", "country": "Россия"}),
    ],
)
def test_defaults_do_not_hide_wrong_city_date_or_extra_arguments(
    action: str,
    expected: dict[str, object],
    actual: dict[str, object],
):
    case: ExpectedDecision = {"expected_action": action, "expected_arguments": expected}

    assert not score_case(case, action, actual, today="2026-10-12")["success"]


def test_baseline_and_extended_datasets_remain_separate():
    baseline = json.loads(routing_eval.DATASET_PATH.read_text(encoding="utf-8"))
    extended_path = routing_eval.DATASET_PATH.with_name("lab2-routing-extended.json")
    extended = json.loads(extended_path.read_text(encoding="utf-8"))

    assert len(baseline["cases"]) == 12
    assert len(extended["cases"]) == 11
    baseline_ids = {case["id"] for case in baseline["cases"]}
    extended_ids = {case["id"] for case in extended["cases"]}
    assert baseline_ids.isdisjoint(extended_ids)

    default_city = next(case for case in extended["cases"] if case["id"] == "weather_default_city")
    assert default_city["expected_action"] == "get_weather"
    assert default_city["expected_arguments"] == {"city": "Санкт-Петербург"}
