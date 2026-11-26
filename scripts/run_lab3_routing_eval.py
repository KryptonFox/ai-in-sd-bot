import argparse
import asyncio
import hashlib
import json
import re
import subprocess
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings, public_url
from app.core.exceptions import LLMError, LLMErrorCode, LLMServiceError, LLMTimeoutError
from app.core.schemas.message import ChatMessage, MessageRole
from app.core.schemas.routing import RouteDecision, RouteTarget
from app.core.trace import OperationTrace, bind_trace
from app.llm.decisions import DecisionsRouter

ROOT = Path(__file__).resolve().parents[1]
OFFICIAL_DATASET = ROOT / "docs/evaluation/lab3-routing.json"
SUPPLEMENTAL_DATASET = ROOT / "docs/evaluation/lab3-routing-supplemental.json"
OFFICIAL_CASE_COUNT = 15
DEFAULT_OUTPUT_DIR = ROOT / "docs/evaluation/results/lab3"

MOCK_PROVIDER = "in-process deterministic fixture"
LIVE_PROVIDER = "OpenCode Go Decisions through DecisionsRouter"


class RunMode(StrEnum):
    MOCK = "mock"
    LIVE = "live"


class MockScenario(StrEnum):
    SUCCESS = "success"
    FAULTS = "faults"


class SuiteName(StrEnum):
    OFFICIAL = "official"
    MATH = "math"
    WEB = "web"
    OVERALL = "overall"


class SupplementalSuite(StrEnum):
    MATH = "math"
    WEB = "web"


class HistoryMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1)
    metadata: dict[str, object] = Field(default_factory=dict)


class EvaluationCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    query: str = Field(min_length=1)
    expected_target: RouteTarget
    history: tuple[HistoryMessage, ...] = ()


class SupplementalCase(EvaluationCase):
    suite: SupplementalSuite
    rationale: str = Field(min_length=1)
    boundary: bool = False


class EvaluationContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    now: str = Field(min_length=1)
    timezone: str = Field(min_length=1)
    city: str = Field(min_length=1)


class OfficialDataset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int
    evaluation_context: EvaluationContext
    cases: tuple[EvaluationCase, ...]


class SupplementalDataset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int
    description: str = Field(min_length=1)
    evaluation_context: EvaluationContext
    cases: tuple[SupplementalCase, ...]


class EvaluationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: RunMode
    mock_scenario: MockScenario = MockScenario.SUCCESS
    official_dataset: Path = OFFICIAL_DATASET
    supplemental_dataset: Path = SUPPLEMENTAL_DATASET
    output_dir: Path = DEFAULT_OUTPUT_DIR
    label: str = Field(min_length=1)
    case_ids: frozenset[str] = frozenset()
    env_file: Path = Path(".env")


class ErrorReport(TypedDict):
    type: str
    code: str | None
    status_code: int | None


class CaseReport(TypedDict):
    id: str
    suite: str
    query: str
    history: list[dict[str, object]]
    expected_target: str
    actual_target: str | None
    confidence: float | None
    correct: bool
    route_ms: float
    total_ms: float
    decision: dict[str, object] | None
    usage: dict[str, int] | None
    allowance: dict[str, object] | None
    error: ErrorReport | None


class ScoreReport(TypedDict):
    suite: str
    total: int
    answered: int
    correct: int
    errors: int
    wrong: int
    accuracy: float
    error_counts: dict[str, int]


class SummaryReport(TypedDict):
    official: ScoreReport
    math: ScoreReport
    web: ScoreReport
    overall: ScoreReport


class RouterReport(TypedDict):
    provider: str
    model: str
    base_url: str
    timeout_seconds: float
    opencode_session: bool
    api_key_configured: bool


class DatasetReport(TypedDict):
    path: str
    sha256: str
    case_count: int


class SelectionReport(TypedDict):
    filtered_by_case_ids: bool
    requested_case_ids: list[str]
    official_expected_total: int
    official_selected_total: int
    official_complete: bool


class MethodReport(TypedDict):
    scope: str
    scoring: str
    denominator: str
    latency: str
    usage: str
    allowance: str


class VerificationReport(TypedDict):
    official_denominator: int
    supplemental_denominators: dict[str, int]
    failures_in_denominator: bool
    suites_scored_separately: bool
    all_selected_cases_reported: bool
    checks_passed: bool


class EvaluationReport(TypedDict):
    schema_version: int
    created_utc: str
    mode: str
    label: str
    revision: str | None
    dirty: bool | None
    app_python_sha256: str
    evaluator_sha256: str
    evaluation_context: dict[str, object]
    router: RouterReport
    datasets: dict[str, DatasetReport]
    selection: SelectionReport
    method: MethodReport
    summary: SummaryReport
    cases: list[CaseReport]
    verification: VerificationReport


@dataclass(frozen=True)
class PlannedCase:
    suite: SuiteName
    case: EvaluationCase
    rationale: str = ""


@dataclass(frozen=True)
class RouteObservation:
    decision: RouteDecision
    usage: dict[str, int] | None = None
    allowance: dict[str, object] | None = None


@dataclass(frozen=True)
class CaseResult:
    case: EvaluationCase
    suite: SuiteName
    actual_target: RouteTarget | None
    confidence: float | None
    correct: bool
    route_ms: float
    total_ms: float
    decision: RouteDecision | None
    usage: dict[str, int] | None
    allowance: dict[str, object] | None
    error: ErrorReport | None


class RouteProvider(Protocol):
    description: str

    async def route(self, case: EvaluationCase) -> RouteObservation: ...

    async def aclose(self) -> None: ...


class DecisionRouter(Protocol):
    async def route(self, text: str, history: Sequence[ChatMessage]) -> RouteDecision: ...

    async def aclose(self) -> None: ...


def _trace_usage(trace: OperationTrace) -> dict[str, int] | None:
    for step in reversed(trace.steps):
        if step.component != "router" or step.input_tokens is None or step.output_tokens is None:
            continue
        return {
            "input_tokens": step.input_tokens,
            "output_tokens": step.output_tokens,
            "total_tokens": step.input_tokens + step.output_tokens,
        }
    return None


class MockRouteProvider:
    def __init__(self, outcomes: Mapping[str, RouteDecision | Exception]) -> None:
        self.description = MOCK_PROVIDER
        self._outcomes = dict(outcomes)

    async def route(self, case: EvaluationCase) -> RouteObservation:
        await asyncio.sleep(0)
        outcome = self._outcomes[case.id]
        if isinstance(outcome, Exception):
            raise outcome
        return RouteObservation(decision=outcome)

    async def aclose(self) -> None:
        return None


class LiveRouteProvider:
    def __init__(
        self,
        settings: Settings,
        router: DecisionRouter | None = None,
    ) -> None:
        self.description = LIVE_PROVIDER
        self._router = router or DecisionsRouter(settings)

    async def route(self, case: EvaluationCase) -> RouteObservation:
        history = tuple(
            ChatMessage(
                role=MessageRole(message.role),
                content=message.content,
                metadata=dict(message.metadata),
            )
            for message in case.history
        )
        trace = OperationTrace(
            user_id=0,
            request_id=f"lab3-eval:{case.id}",
            started_at=datetime.now(UTC),
        )
        with bind_trace(trace):
            decision = await self._router.route(case.query, history)
        return RouteObservation(decision=decision, usage=_trace_usage(trace))

    async def aclose(self) -> None:
        await self._router.aclose()


def _json_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(ROOT).as_posix()
    except ValueError:
        return str(resolved)


def _read_text(path: Path) -> tuple[str, str]:
    text = path.read_text(encoding="utf-8")
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()


def _ensure_unique_case_ids(cases: Sequence[EvaluationCase]) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for case in cases:
        if case.id in seen:
            duplicates.add(case.id)
        seen.add(case.id)
    if duplicates:
        raise ValueError(f"duplicate case ids: {', '.join(sorted(duplicates))}")


def load_official_dataset(
    path: Path = OFFICIAL_DATASET,
) -> tuple[OfficialDataset, DatasetReport]:
    text, digest = _read_text(path)
    dataset = OfficialDataset.model_validate_json(text)
    if len(dataset.cases) != OFFICIAL_CASE_COUNT:
        raise ValueError(
            f"official dataset must contain {OFFICIAL_CASE_COUNT} cases, found {len(dataset.cases)}"
        )
    _ensure_unique_case_ids(dataset.cases)
    return dataset, DatasetReport(
        path=_json_path(path),
        sha256=digest,
        case_count=len(dataset.cases),
    )


def load_supplemental_dataset(
    path: Path = SUPPLEMENTAL_DATASET,
) -> tuple[SupplementalDataset, DatasetReport]:
    text, digest = _read_text(path)
    dataset = SupplementalDataset.model_validate_json(text)
    if not dataset.cases:
        raise ValueError("supplemental dataset must contain at least one case")
    _ensure_unique_case_ids(dataset.cases)
    return dataset, DatasetReport(
        path=_json_path(path),
        sha256=digest,
        case_count=len(dataset.cases),
    )


def build_planned_cases(
    official: OfficialDataset,
    supplemental: SupplementalDataset,
    case_ids: frozenset[str],
) -> list[PlannedCase]:
    planned = [PlannedCase(SuiteName.OFFICIAL, case) for case in official.cases]
    planned.extend(
        PlannedCase(SuiteName(case.suite.value), case, case.rationale)
        for case in supplemental.cases
    )
    _ensure_unique_case_ids([item.case for item in planned])

    if not case_ids:
        return planned

    known = {item.case.id for item in planned}
    unknown = case_ids - known
    if unknown:
        raise ValueError(f"unknown case ids: {', '.join(sorted(unknown))}")

    selected = [item for item in planned if item.case.id in case_ids]
    if not selected:
        raise ValueError("case selection is empty")
    return selected


def _fixture_decision(target: RouteTarget) -> RouteDecision:
    return RouteDecision(target=target, reason="mock_fixture", confidence=0.95)


def _wrong_target(expected: RouteTarget) -> RouteTarget:
    for candidate in RouteTarget:
        if candidate is not expected:
            return candidate
    return RouteTarget.CLARIFY


def build_mock_outcomes(
    planned: Sequence[PlannedCase],
    scenario: MockScenario,
) -> dict[str, RouteDecision | Exception]:
    outcomes: dict[str, RouteDecision | Exception] = {
        item.case.id: _fixture_decision(item.case.expected_target) for item in planned
    }
    if scenario is MockScenario.SUCCESS:
        return outcomes

    official = [item for item in planned if item.suite is SuiteName.OFFICIAL]
    math = [item for item in planned if item.suite is SuiteName.MATH]
    web = [item for item in planned if item.suite is SuiteName.WEB]

    if official:
        first = official[0]
        outcomes[first.case.id] = _fixture_decision(_wrong_target(first.case.expected_target))
    if len(official) > 1:
        outcomes[official[1].case.id] = LLMTimeoutError()
    if math:
        outcomes[math[0].case.id] = LLMServiceError(
            "fixture rate limit",
            code=LLMErrorCode.RATE_LIMIT,
            status_code=429,
        )
    if web:
        outcomes[web[0].case.id] = _fixture_decision(_wrong_target(web[0].case.expected_target))
    return outcomes


def _case_error(error: Exception) -> ErrorReport:
    if isinstance(error, LLMError):
        return ErrorReport(
            type=type(error).__name__,
            code=error.code.value,
            status_code=error.status_code,
        )
    return ErrorReport(type=type(error).__name__, code=None, status_code=None)


async def evaluate_cases(
    provider: RouteProvider,
    planned: Sequence[PlannedCase],
) -> list[CaseResult]:
    results: list[CaseResult] = []
    for item in planned:
        started = time.perf_counter()
        observation: RouteObservation | None = None
        failure: ErrorReport | None = None
        try:
            observation = await provider.route(item.case)
        except Exception as error:
            failure = _case_error(error)

        total_ms = round((time.perf_counter() - started) * 1000, 3)
        decision = observation.decision if observation is not None else None
        actual_target = decision.target if decision is not None else None
        results.append(
            CaseResult(
                case=item.case,
                suite=item.suite,
                actual_target=actual_target,
                confidence=decision.confidence if decision is not None else None,
                correct=failure is None and actual_target is item.case.expected_target,
                route_ms=total_ms,
                total_ms=total_ms,
                decision=decision,
                usage=observation.usage if observation is not None else None,
                allowance=observation.allowance if observation is not None else None,
                error=failure,
            )
        )
        outcome = "error" if failure is not None else str(actual_target)
        print(f"{item.case.id}: {outcome} ({total_ms:.1f} ms)", flush=True)
    return results


def score_results(suite: SuiteName, results: Sequence[CaseResult]) -> ScoreReport:
    total = len(results)
    errors = sum(1 for result in results if result.error is not None)
    correct = sum(1 for result in results if result.correct)
    answered = total - errors
    error_counts = Counter(
        result.error["code"] or result.error["type"]
        for result in results
        if result.error is not None
    )
    return ScoreReport(
        suite=suite.value,
        total=total,
        answered=answered,
        correct=correct,
        errors=errors,
        wrong=answered - correct,
        accuracy=correct / total if total else 0.0,
        error_counts=dict(error_counts),
    )


def build_summary(results: Sequence[CaseResult]) -> SummaryReport:
    official = [result for result in results if result.suite is SuiteName.OFFICIAL]
    math = [result for result in results if result.suite is SuiteName.MATH]
    web = [result for result in results if result.suite is SuiteName.WEB]
    return SummaryReport(
        official=score_results(SuiteName.OFFICIAL, official),
        math=score_results(SuiteName.MATH, math),
        web=score_results(SuiteName.WEB, web),
        overall=score_results(SuiteName.OVERALL, results),
    )


def build_verification(
    results: Sequence[CaseResult],
    summary: SummaryReport,
    planned: Sequence[PlannedCase],
) -> VerificationReport:
    scores = [
        summary["official"],
        summary["math"],
        summary["web"],
        summary["overall"],
    ]
    failures_in_denominator = all(
        score["total"] == score["correct"] + score["wrong"] + score["errors"]
        and score["total"] == score["answered"] + score["errors"]
        for score in scores
    )
    expected_suites = {item.suite for item in planned}
    actual_suites = {result.suite for result in results}
    suites_scored_separately = actual_suites == expected_suites
    all_selected_cases_reported = len(results) == len(planned)
    checks_passed = (
        failures_in_denominator and suites_scored_separately and all_selected_cases_reported
    )
    return VerificationReport(
        official_denominator=summary["official"]["total"],
        supplemental_denominators={
            "math": summary["math"]["total"],
            "web": summary["web"]["total"],
        },
        failures_in_denominator=failures_in_denominator,
        suites_scored_separately=suites_scored_separately,
        all_selected_cases_reported=all_selected_cases_reported,
        checks_passed=checks_passed,
    )


def _case_report(result: CaseResult) -> CaseReport:
    return CaseReport(
        id=result.case.id,
        suite=result.suite.value,
        query=result.case.query,
        history=[message.model_dump(mode="json") for message in result.case.history],
        expected_target=result.case.expected_target.value,
        actual_target=result.actual_target.value if result.actual_target is not None else None,
        confidence=result.confidence,
        correct=result.correct,
        route_ms=result.route_ms,
        total_ms=result.total_ms,
        decision=result.decision.model_dump(mode="json") if result.decision else None,
        usage=result.usage,
        allowance=result.allowance,
        error=result.error,
    )


def _method_report(mode: RunMode) -> MethodReport:
    scope = (
        "One DecisionsRouter.route call per synthetic input. No specialist, LLM generation, "
        "MCP tool, weather, knowledge search, web search, database, or Telegram action runs."
        if mode is RunMode.LIVE
        else "Mock fixture through the same evaluation loop. No network request, DecisionsRouter, "
        "specialist, MCP tool, or Telegram action runs."
    )
    return MethodReport(
        scope=scope,
        scoring=(
            "Exact target match. Wrong routes, provider errors, and unanswered cases are incorrect."
        ),
        denominator=(
            "Every selected case stays in its suite denominator, including transport errors, "
            "refusals, timeouts, and invalid responses. The official suite is expected to "
            "contain 15 cases."
        ),
        latency=(
            "Per-case wall time around the provider call; total equals route time in "
            "route-only mode."
        ),
        usage=(
            "Input and output token counts are read from the nested router api_request trace "
            "step when DecisionsRouter and the provider supply them; absent counts stay null."
        ),
        allowance=(
            "Quota allowance is not claimed: DecisionsRouter does not return opencode.allowance "
            "and the evaluator makes no extra billing request."
        ),
    )


def _router_report(mode: RunMode, settings: Settings) -> RouterReport:
    if mode is RunMode.LIVE:
        return RouterReport(
            provider=LIVE_PROVIDER,
            model=settings.router_model,
            base_url=public_url(settings.router_base_url),
            timeout_seconds=settings.router_timeout,
            opencode_session=settings.router_opencode_session,
            api_key_configured=bool(settings.effective_router_api_key),
        )
    return RouterReport(
        provider=MOCK_PROVIDER,
        model="fixture",
        base_url="",
        timeout_seconds=0.0,
        opencode_session=False,
        api_key_configured=False,
    )


def _selection_report(
    official: OfficialDataset,
    planned: Sequence[PlannedCase],
    case_ids: frozenset[str],
) -> SelectionReport:
    selected = [item for item in planned if item.suite is SuiteName.OFFICIAL]
    return SelectionReport(
        filtered_by_case_ids=bool(case_ids),
        requested_case_ids=sorted(case_ids),
        official_expected_total=OFFICIAL_CASE_COUNT,
        official_selected_total=len(selected),
        official_complete=len(selected) == len(official.cases),
    )


def _revision() -> str | None:
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
    for source_root in (ROOT / "app", ROOT / "mcp_servers"):
        for path in sorted(source_root.rglob("*.py")):
            relative = path.relative_to(ROOT).as_posix().encode("utf-8")
            content = path.read_bytes()
            digest.update(len(relative).to_bytes(8, "big"))
            digest.update(relative)
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
    return digest.hexdigest()


def _evaluator_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _safe_label(label: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", label).strip("-._") or "run"


def write_report(report: EvaluationReport, output_dir: Path, created_at: datetime) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = created_at.strftime("%Y%m%dT%H%M%S%fZ")
    path = output_dir / f"lab3-routing-{_safe_label(report['label'])}-{stamp}.json"
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    json.loads(text)
    path.write_text(text, encoding="utf-8")
    return path


async def run_evaluation(config: EvaluationConfig) -> tuple[EvaluationReport, Path]:
    official, official_report = load_official_dataset(config.official_dataset)
    supplemental, supplemental_report = load_supplemental_dataset(config.supplemental_dataset)
    planned = build_planned_cases(official, supplemental, config.case_ids)

    settings = (
        Settings.load(config.env_file)
        if config.mode is RunMode.LIVE
        else Settings(bot_token="", postgres_password="")
    )
    provider: RouteProvider
    if config.mode is RunMode.LIVE:
        provider = LiveRouteProvider(settings)
    else:
        provider = MockRouteProvider(build_mock_outcomes(planned, config.mock_scenario))

    try:
        results = await evaluate_cases(provider, planned)
    finally:
        await provider.aclose()

    summary = build_summary(results)
    created_at = datetime.now(UTC)
    report = EvaluationReport(
        schema_version=1,
        created_utc=created_at.isoformat(),
        mode=config.mode.value,
        label=config.label,
        revision=_revision(),
        dirty=_dirty(),
        app_python_sha256=_app_python_sha256(),
        evaluator_sha256=_evaluator_sha256(),
        evaluation_context=official.evaluation_context.model_dump(mode="json"),
        router=_router_report(config.mode, settings),
        datasets={
            "official": official_report,
            "supplemental": supplemental_report,
        },
        selection=_selection_report(official, planned, config.case_ids),
        method=_method_report(config.mode),
        summary=summary,
        cases=[_case_report(result) for result in results],
        verification=build_verification(results, summary, planned),
    )
    path = write_report(report, config.output_dir, created_at)
    return report, path


def _score_line(name: str, score: ScoreReport) -> str:
    return f"{name}: {score['correct']}/{score['total']} ({score['accuracy']:.1%})"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the reproducible Lab 3 routing evaluation.")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Allow real OpenCode Decisions API calls. Without it the evaluator uses fixtures.",
    )
    parser.add_argument(
        "--mock-scenario",
        choices=[scenario.value for scenario in MockScenario],
        default=MockScenario.SUCCESS.value,
        help="Fixture behavior for a non-live run.",
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--label", help="Report label; defaults to the run mode.")
    parser.add_argument(
        "--case",
        action="append",
        default=[],
        help="Case id to run; repeat to select several. Defaults to both datasets.",
    )
    parser.add_argument("--official-dataset", type=Path, default=OFFICIAL_DATASET)
    parser.add_argument("--supplemental-dataset", type=Path, default=SUPPLEMENTAL_DATASET)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    mode = RunMode.LIVE if args.live else RunMode.MOCK
    if mode is RunMode.LIVE and args.mock_scenario != MockScenario.SUCCESS.value:
        parser.error("--mock-scenario applies only to non-live runs")

    label = args.label or ("live" if mode is RunMode.LIVE else f"mock-{args.mock_scenario}")
    config = EvaluationConfig(
        mode=mode,
        mock_scenario=MockScenario(args.mock_scenario),
        official_dataset=args.official_dataset,
        supplemental_dataset=args.supplemental_dataset,
        output_dir=args.output_dir,
        label=label,
        case_ids=frozenset(args.case),
        env_file=args.env_file,
    )
    report, path = asyncio.run(run_evaluation(config))
    print(_score_line("official", report["summary"]["official"]))
    print(_score_line("math", report["summary"]["math"]))
    print(_score_line("web", report["summary"]["web"]))
    print(_score_line("overall", report["summary"]["overall"]))
    print(f"report: {_json_path(path)}")


if __name__ == "__main__":
    main()
