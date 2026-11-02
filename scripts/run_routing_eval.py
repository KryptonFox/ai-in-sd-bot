import argparse
import asyncio
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NotRequired, TypedDict, cast

from app.core.clock import FrozenClock
from app.core.context import set_request_id
from app.core.exceptions import LLMError
from app.core.schemas.agent import AgentDecision, AgentResult, EventContext
from app.core.schemas.message import ChatMessage, MessageRole
from app.core.schemas.tools import DEFAULT_WEATHER_CITY

if TYPE_CHECKING:
    from app.modes.base import BotMode
    from app.services.agent_service import AgentService

ROOT = Path(__file__).resolve().parents[1]
DATASET_PATH = ROOT / "docs/evaluation/lab2-routing.json"
TOOL_ACTIONS = {"get_weather", "get_schedule", "get_schedule_week", "read_resource", "add_reminder"}


class MeasurementStatus(StrEnum):
    COMPLETED = "completed"
    INCOMPLETE = "incomplete"
    UNAVAILABLE = "unavailable"


class ExpectedDecision(TypedDict):
    expected_action: str
    expected_arguments: dict[str, object]


class HistoryEntry(TypedDict):
    role: Literal["user", "assistant"]
    content: str


class RoutingCase(ExpectedDecision):
    id: str
    query: str
    history: NotRequired[list[HistoryEntry]]


class EvaluationContext(TypedDict):
    now: str
    timezone: str


class EvaluationDataset(TypedDict):
    version: int
    evaluation_context: EvaluationContext
    cases: list[RoutingCase]


class ScoredDecision(TypedDict):
    route_correct: bool
    arguments_applicable: bool
    arguments_correct: bool | None


class CaseScore(ScoredDecision):
    success: bool


class CaseResult(CaseScore, ExpectedDecision):
    id: str
    predicted_action: str
    arguments: dict[str, object]
    effective_arguments: NotRequired[dict[str, object]]
    error_class: NotRequired[str]
    error_code: NotRequired[str]
    status_code: NotRequired[int]
    public_answer: NotRequired[str]


class EvaluationSummary(TypedDict):
    routing_correct: int
    routing_total: int
    routing_accuracy: float
    arguments_correct: int
    arguments_total: int
    arguments_accuracy: float


class EvaluationCounts(TypedDict):
    planned: int
    attempted: int
    decisions_received: int
    not_run: int


class MarkdownReport(TypedDict):
    label: str
    measurement_status: MeasurementStatus
    counts: EvaluationCounts
    summary: EvaluationSummary
    cases: list[CaseResult]


class EvaluationReport(MarkdownReport):
    model: str
    temperature: float
    clock: str
    timezone: str
    revision: str | None
    dirty: bool | None
    app_python_sha256: str
    dataset: str
    dataset_sha256: str
    evaluator_sha256: str
    created_at: str
    max_tool_calls: int
    max_llm_steps: int


def _datetime_instant(value: object) -> str | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None

    return parsed.astimezone(UTC).isoformat()


def normalize_value(key: str, value: object) -> object:
    if key == "remind_at":
        instant = _datetime_instant(value)
        return instant if instant is not None else value

    if key == "city" and isinstance(value, str):
        return value.strip().casefold()

    if key == "text" and isinstance(value, str):
        return value.strip().casefold()

    if isinstance(value, str):
        return value.strip()

    return value


def normalize_arguments(
    arguments: Mapping[str, object] | None,
) -> dict[str, object]:
    if arguments is None:
        return {}

    return {
        key: normalize_value(key, value)
        for key, value in arguments.items()
        if key != "date" or value is not None
    }


def effective_arguments(
    action: str,
    arguments: Mapping[str, object],
    today: str | None,
) -> dict[str, object]:
    normalized = normalize_arguments(arguments)

    if action == "get_weather":
        normalized.setdefault("city", normalize_value("city", DEFAULT_WEATHER_CITY))

    if action in {"get_weather", "get_schedule"} and today is not None:
        normalized.setdefault("date", today)

    return normalized


def score_case(
    case: ExpectedDecision,
    predicted_action: str,
    arguments: Mapping[str, object],
    *,
    today: str | None = None,
) -> CaseScore:
    expected_action = case["expected_action"]
    expected_arguments = case.get("expected_arguments", {})
    route_correct = predicted_action == expected_action
    args_applicable = expected_action in TOOL_ACTIONS
    actual = effective_arguments(predicted_action, arguments, today)
    expected = effective_arguments(expected_action, expected_arguments, today)

    args_correct = None
    if args_applicable:
        args_correct = route_correct and actual == expected

    return {
        "route_correct": route_correct,
        "arguments_applicable": args_applicable,
        "arguments_correct": args_correct,
        "success": route_correct and (args_correct is not False),
    }


def summarize_cases(cases: Sequence[ScoredDecision]) -> EvaluationSummary:
    route_total = len(cases)
    route_correct = sum(bool(case["route_correct"]) for case in cases)
    argument_cases = [case for case in cases if case["arguments_applicable"]]
    argument_correct = sum(bool(case["arguments_correct"]) for case in argument_cases)

    return {
        "routing_correct": route_correct,
        "routing_total": route_total,
        "routing_accuracy": route_correct / route_total if route_total else 0.0,
        "arguments_correct": argument_correct,
        "arguments_total": len(argument_cases),
        "arguments_accuracy": argument_correct / len(argument_cases) if argument_cases else 0.0,
    }


def _action_for_result(result: AgentResult) -> str:
    if result.decision in {AgentDecision.RESPOND, AgentDecision.CLARIFY}:
        return result.decision

    if result.decision in {AgentDecision.TOOL, AgentDecision.PREPARE}:
        return result.tool_name or result.decision

    return "error"


def _is_availability_failure(error: Exception) -> bool:
    connection_types = {
        "APIConnectionError",
        "ConnectError",
        "ConnectionError",
        "LLMConnectionError",
    }
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        if isinstance(current, LLMError) and current.status_code in {502, 503, 504}:
            return True

        if type(current).__name__ in connection_types:
            return True

        seen.add(id(current))
        current = current.__cause__ or current.__context__

    return False


def _markdown_report(result: MarkdownReport) -> str:
    summary = result["summary"]
    counts = result["counts"]
    if result["measurement_status"] == MeasurementStatus.COMPLETED:
        interpretation = "All selected cases returned a decision."
    elif result["measurement_status"] == MeasurementStatus.INCOMPLETE:
        interpretation = (
            "Some selected cases did not return a decision; the displayed scores do not measure "
            "model quality."
        )
    else:
        interpretation = (
            "No decisions were received. The displayed zero scores reflect missing responses, "
            "not measured model quality."
        )

    lines = [
        f"# Routing evaluation: {result['label']}",
        "",
        f"Measurement status: **{result['measurement_status']}**",
        f"Decisions received: {counts['decisions_received']}/{counts['planned']}",
        f"Cases attempted: {counts['attempted']}/{counts['planned']}",
        f"Cases not run: {counts['not_run']}",
        interpretation,
        "Scope: dry-run routing and arguments only; "
        "geocoding, forecast and final weather answers are not measured.",
        "Scoring applies the same city/date defaults to expected and predicted arguments; "
        "raw and effective model arguments are retained in JSON.",
        "",
        "Routing: "
        f"{summary['routing_correct']}/{summary['routing_total']} "
        f"({summary['routing_accuracy']:.1%})",
        "Arguments: "
        f"{summary['arguments_correct']}/{summary['arguments_total']} "
        f"({summary['arguments_accuracy']:.1%})",
        "",
        "| Case | Expected | Predicted | Route | Arguments | Error |",
        "| --- | --- | --- | --- | --- | --- |",
    ]

    for case in result["cases"]:
        expected = case["expected_action"]
        predicted = case.get("predicted_action", "error")
        route = "✓" if case.get("route_correct") else "✗"
        args = (
            "—"
            if case["arguments_applicable"] is False
            else ("✓" if case.get("arguments_correct") else "✗")
        )
        error = case.get("error_class", "")
        lines.append(f"| {case['id']} | {expected} | {predicted} | {route} | {args} | {error} |")

    return "\n".join(lines) + "\n"


async def run_evaluation(
    case_ids: set[str] | None,
    output_dir: Path,
    label: str,
    *,
    dataset_path: Path | None = None,
) -> Path:
    dataset_path = dataset_path or DATASET_PATH
    dataset_text = await asyncio.to_thread(dataset_path.read_text, encoding="utf-8")
    dataset = cast(EvaluationDataset, json.loads(dataset_text))
    cases = dataset["cases"]
    if case_ids is not None:
        known = {case["id"] for case in cases}
        unknown = case_ids - known
        if unknown:
            raise ValueError(f"Unknown case ids: {', '.join(sorted(unknown))}")

        cases = [case for case in cases if case["id"] in case_ids]

    from app.config import Settings
    from app.llm.client import OpenAIClient
    from app.mcp.client import MCPClient
    from app.mcp_server.server import resource_catalog, tool_catalog
    from app.modes.definitions import STUDY_MODE
    from app.services.agent_service import AgentService
    from app.services.tool_execution import ToolExecutionService

    settings = Settings.load()
    from app.logging_setup import configure_logging

    configure_logging(settings)
    client = OpenAIClient(settings)
    mcp = MCPClient(settings)
    mcp.load_catalog(tool_catalog())
    mcp.load_resources(resource_catalog())
    service = AgentService(
        client,
        ToolExecutionService(
            mcp,
            clock=FrozenClock(datetime.fromisoformat(dataset["evaluation_context"]["now"])),
            max_calls=settings.agent_max_tool_calls,
        ),
        clock=FrozenClock(datetime.fromisoformat(dataset["evaluation_context"]["now"])),
        max_llm_steps=settings.agent_max_llm_steps,
    )

    try:
        result_cases, counts = await _evaluate_cases(
            service,
            cases,
            dataset["evaluation_context"],
            settings.llm_model,
            STUDY_MODE,
        )
    finally:
        await client.aclose()
        await mcp.aclose()

    summary = summarize_cases(result_cases)
    measurement_status: MeasurementStatus
    if counts["planned"] > 0 and counts["decisions_received"] == counts["planned"]:
        measurement_status = MeasurementStatus.COMPLETED
    elif counts["decisions_received"] > 0:
        measurement_status = MeasurementStatus.INCOMPLETE
    else:
        measurement_status = MeasurementStatus.UNAVAILABLE

    created_at = datetime.now(UTC)
    evaluator_source = await asyncio.to_thread(Path(__file__).read_bytes)
    output: EvaluationReport = {
        "label": label,
        "model": settings.llm_model,
        "temperature": 0.0,
        "clock": dataset["evaluation_context"]["now"],
        "timezone": dataset["evaluation_context"]["timezone"],
        "revision": _revision(),
        "dirty": _dirty(),
        "app_python_sha256": _app_python_sha256(),
        "dataset": dataset_path.name,
        "dataset_sha256": hashlib.sha256(dataset_text.encode()).hexdigest(),
        "evaluator_sha256": hashlib.sha256(evaluator_source).hexdigest(),
        "created_at": created_at.isoformat(),
        "max_tool_calls": settings.agent_max_tool_calls,
        "max_llm_steps": settings.agent_max_llm_steps,
        "measurement_status": measurement_status,
        "counts": counts,
        "summary": summary,
        "cases": result_cases,
    }

    return await _write_report(output, output_dir, created_at)


async def _evaluate_cases(
    service: "AgentService",
    cases: list[RoutingCase],
    evaluation_context: EvaluationContext,
    model: str,
    mode: "BotMode",
) -> tuple[list[CaseResult], EvaluationCounts]:
    today = datetime.fromisoformat(evaluation_context["now"]).date().isoformat()
    result_cases: list[CaseResult] = []
    availability_failures = 0
    stopped = False
    attempted = 0
    decisions_received = 0

    for case in cases:
        row: CaseResult = {
            "id": case["id"],
            "expected_action": case["expected_action"],
            "expected_arguments": case.get("expected_arguments", {}),
            "predicted_action": "error",
            "arguments": {},
            "route_correct": False,
            "arguments_applicable": case["expected_action"] in TOOL_ACTIONS,
            "arguments_correct": False if case["expected_action"] in TOOL_ACTIONS else None,
            "success": False,
        }
        if stopped:
            row["error_class"] = "NotRunAfterFailure"
            result_cases.append(row)
            continue

        context = EventContext(
            user_id=1,
            event_id=f"eval:{case['id']}",
            request_id=f"eval:{case['id']}",
        )
        set_request_id(context.request_id)
        messages = mode.build_initial_messages(case["query"])
        messages.extend(
            ChatMessage(role=MessageRole(entry["role"]), content=entry["content"])
            for entry in case.get("history", [])
        )
        messages.append(ChatMessage(role=MessageRole.USER, content=case["query"]))

        try:
            attempted += 1
            agent_result = await service.run(
                messages,
                context,
                timezone=evaluation_context["timezone"],
                temperature=0.0,
                model=model,
                dry_run=True,
            )
            decisions_received += 1
            predicted_action = _action_for_result(agent_result)
            predicted_arguments = dict(agent_result.arguments)
            row["predicted_action"] = predicted_action
            row["public_answer"] = agent_result.text
            row["arguments"] = predicted_arguments
            row["effective_arguments"] = effective_arguments(
                predicted_action,
                predicted_arguments,
                today,
            )
            score = score_case(
                case,
                predicted_action,
                predicted_arguments,
                today=today,
            )
            row["route_correct"] = score["route_correct"]
            row["arguments_applicable"] = score["arguments_applicable"]
            row["arguments_correct"] = score["arguments_correct"]
            row["success"] = score["success"]
            if agent_result.decision == AgentDecision.ERROR:
                row["error_class"] = "AgentResultError"
        except Exception as exc:
            row["error_class"] = type(exc).__name__
            if isinstance(exc, LLMError):
                row["error_code"] = exc.code
                if exc.status_code is not None:
                    row["status_code"] = exc.status_code

            if _is_availability_failure(exc):
                availability_failures += 1
                stopped = availability_failures >= 2
            else:
                stopped = True

        result_cases.append(row)
        print(f"{case['id']}: {row['predicted_action']} {row.get('error_class', '')}", flush=True)

    counts: EvaluationCounts = {
        "planned": len(cases),
        "attempted": attempted,
        "decisions_received": decisions_received,
        "not_run": len(cases) - attempted,
    }

    return result_cases, counts


async def _write_report(
    output: EvaluationReport,
    output_dir: Path,
    created_at: datetime,
) -> Path:
    label = output["label"]
    stamp = created_at.strftime("%Y%m%dT%H%M%S%fZ")
    safe_label = (
        re.sub(
            r"[^A-Za-z0-9_.-]+",
            "-",
            label,
        ).strip("-._")
        or "baseline"
    )
    await asyncio.to_thread(output_dir.mkdir, parents=True, exist_ok=True)
    stem = f"lab2-routing-{safe_label}-{stamp}"
    json_path = output_dir / f"{stem}.json"
    json_text = json.dumps(output, ensure_ascii=False, indent=2) + "\n"
    markdown_text = _markdown_report(output)
    await asyncio.to_thread(json_path.write_text, json_text, encoding="utf-8")
    await asyncio.to_thread(
        (output_dir / f"{stem}.md").write_text,
        markdown_text,
        encoding="utf-8",
    )

    return json_path


def _revision() -> str | None:
    import subprocess

    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _dirty() -> bool | None:
    import subprocess

    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        return bool(result.stdout.strip())
    except (OSError, subprocess.CalledProcessError):
        return None


def _app_python_sha256() -> str:
    digest = hashlib.sha256()
    app_root = ROOT / "app"

    for path in sorted(app_root.rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)

    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Lab 2 routing evaluation.")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Allow paid model API calls.",
    )
    parser.add_argument(
        "--cases",
        nargs="*",
        help="Case ids to run; defaults to the full dataset.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "docs/evaluation/results/lab2",
    )
    parser.add_argument("--label", default="baseline")
    parser.add_argument("--dataset", type=Path, default=DATASET_PATH, help="Routing dataset JSON.")
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required to run evaluation cases")

    case_ids = set(args.cases) if args.cases else None
    output = asyncio.run(
        run_evaluation(case_ids, args.output_dir, args.label, dataset_path=args.dataset)
    )
    print(output)


if __name__ == "__main__":
    main()
