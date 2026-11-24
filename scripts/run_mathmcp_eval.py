import argparse
import asyncio
import base64
import hashlib
import json
from collections.abc import Sequence
from contextlib import AsyncExitStack
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from time import monotonic
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from app.config import MCPTransport, Settings
from app.core.budget import ContextBudget
from app.core.exceptions import LLMError
from app.core.ports import ToolClient
from app.core.schemas.agent import AgentDecision, EventContext
from app.core.schemas.execution import RunState
from app.core.schemas.llm import FinishReason, LLMRequest, LLMResponse
from app.core.schemas.message import ChatMessage, MessageRole, ToolCall
from app.core.schemas.tools import PhotoToolResult, ToolFailure, ToolImage
from app.llm.base import BaseLLMClient
from app.llm.client import OpenAIClient
from app.mcp.client import MCPClient
from app.mcp.connection import create_math_tools_client, create_tool_clients
from app.modes.definitions import STUDY_MODE
from app.services.agent_service import AgentService
from app.services.tool_execution import ToolExecutionService

DATASET = Path("docs/evaluation/plan10/mathmcp.json")
OPENCODE_HOST = "opencode.ai"
OPENCODE_MODEL = "deepseek-v4.1-flash"


class ReferenceCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    arguments: dict[str, object]
    expected: dict[str, object] = Field(default_factory=dict)
    expected_items: dict[str, list[dict[str, object]]] = Field(default_factory=dict)


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    query: str
    reference: str
    direct: list[ReferenceCall]
    decision: AgentDecision = AgentDecision.TOOL
    image: bool = False
    mandatory: bool = False


class Dataset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int
    cases: list[Case]


@dataclass
class Step:
    duration_ms: int
    max_tokens: int | None
    finish: FinishReason
    prompt_tokens: int | None
    completion_tokens: int | None
    calls: list[dict[str, object]]


def elapsed_ms(start: float) -> int:
    return round((monotonic() - start) * 1000)


class RecordingLLM:
    def __init__(self, client: BaseLLMClient) -> None:
        self.client = client
        self.steps: list[Step] = []

    async def generate(self, request: LLMRequest) -> LLMResponse:
        started = monotonic()
        response = await self.client.generate(request)
        self.steps.append(
            Step(
                elapsed_ms(started),
                request.max_tokens,
                response.finish_reason,
                response.prompt_tokens,
                response.completion_tokens,
                [
                    {"name": call.name, "arguments": dict(call.arguments)}
                    for call in response.tool_calls
                ],
            )
        )
        return response

    async def get_context_length(self, model: str | None = None) -> int:
        return await self.client.get_context_length(model)

    async def aclose(self) -> None:
        await self.client.aclose()


@dataclass
class CallResult:
    name: str
    duration_ms: int
    arguments: dict[str, object]
    output: object = None
    error: str | None = None
    images: list[str] = field(default_factory=list)
    matches: bool | None = None


class RecordingTools(ToolExecutionService):
    def __init__(self, mcp: ToolClient, settings: Settings, math_names: frozenset[str]) -> None:
        super().__init__(
            mcp,
            max_calls=settings.agent_max_tool_calls,
            max_output_chars=settings.tool_result_max_chars,
        )
        self.calls: list[CallResult] = []
        self.math_names = math_names

    async def _read(self, state: RunState, call: ToolCall) -> None:
        started = monotonic()
        row = CallResult(call.name, 0, dict(call.arguments))
        self.calls.append(row)
        try:
            if call.name not in self.math_names:
                raise ToolFailure("unexpected_evaluation_tool")
            await super()._read(state, call)
            row.output = json.loads(state.conversation[-1].content)["data"]
        except ToolFailure as error:
            row.error = error.code
            raise
        finally:
            row.duration_ms = elapsed_ms(started)

    async def _read_resource_call(self, state: RunState, call: ToolCall, uri: str) -> None:
        raise ToolFailure("unexpected_evaluation_resource")


def matches_output(expected: object, actual: object) -> bool:
    if isinstance(expected, dict) and isinstance(actual, dict):
        return all(
            key in actual and matches_output(value, actual[key]) for key, value in expected.items()
        )
    if isinstance(expected, list) and isinstance(actual, list):
        return len(expected) == len(actual) and all(
            matches_output(left, right) for left, right in zip(expected, actual, strict=True)
        )
    return expected == actual


def matches_reference(call: ReferenceCall, actual: object) -> bool:
    if not isinstance(actual, dict) or not matches_output(call.expected, actual):
        return False

    for field_name, items in call.expected_items.items():
        actual_items = actual.get(field_name)
        if not isinstance(actual_items, list):
            return False
        if not all(any(matches_output(item, value) for value in actual_items) for item in items):
            return False

    return True


def save_images(output: Path, stem: str, images: Sequence[ToolImage]) -> list[str]:
    names: list[str] = []
    for index, image in enumerate(images, 1):
        name = f"{stem}-{index}.png"
        (output / name).write_bytes(base64.b64decode(image.data, validate=True))
        names.append(name)
    return names


async def direct_case(mcp: MCPClient, case: Case, output: Path) -> dict[str, object]:
    rows: list[CallResult] = []
    for index, call in enumerate(case.direct, 1):
        started = monotonic()
        row = CallResult(call.name, 0, call.arguments)
        rows.append(row)
        if call.name not in mcp.tools:
            row.error = "tool_not_discovered"
            row.matches = False
            continue
        try:
            outcome = await mcp.call_tool(call.name, call.arguments)
            if isinstance(outcome, PhotoToolResult):
                row.output = outcome.payload
                row.images = save_images(output, f"{case.id}-direct-{index}", outcome.images)
            else:
                row.output = outcome
        except ToolFailure as error:
            row.error = error.code
        row.duration_ms = elapsed_ms(started)
        row.matches = row.error is None and matches_reference(call, row.output)

    matches = all(row.matches for row in rows) and (
        not case.image or any(row.images for row in rows)
    )
    return {"matched": matches if rows else None, "calls": [asdict(row) for row in rows]}


async def live_case(
    mcp: ToolClient,
    llm: RecordingLLM,
    settings: Settings,
    case: Case,
    output: Path,
    budget: ContextBudget,
    math_names: frozenset[str],
) -> dict[str, object]:
    llm.steps.clear()
    tools = RecordingTools(mcp, settings, math_names)
    service = AgentService(llm, tools, max_llm_steps=settings.agent_max_llm_steps)
    messages = [
        *STUDY_MODE.build_initial_messages(case.query),
        ChatMessage(role=MessageRole.USER, content=case.query),
    ]
    context = EventContext(user_id=1, event_id=case.id, request_id=f"mathmcp-eval-{case.id}")
    started = monotonic()
    deadline = asyncio.get_running_loop().time() + settings.agent_timeout
    result: dict[str, object] = {}
    try:
        async with asyncio.timeout_at(deadline):
            answer = await service.run(
                messages,
                context,
                timezone="Europe/Moscow",
                temperature=0.0,
                budget=budget,
                protected_prefix=len(messages) - 1,
                deadline_at=deadline,
            )
        result.update(
            decision=answer.decision,
            decision_matches=answer.decision == case.decision,
            reason=answer.reason,
            answer=answer.text,
            truncated=answer.truncated,
            images=save_images(output, f"{case.id}-live", answer.images),
        )
    except LLMError as error:
        result.update(error=error.code, status_code=error.status_code)
    except TimeoutError:
        result.update(error="agent_timeout")
    result.update(
        duration_ms=elapsed_ms(started),
        steps=[asdict(step) for step in llm.steps],
        calls=[asdict(call) for call in tools.calls],
    )
    return result


def validate_live_profile(settings: Settings) -> None:
    url = urlsplit(settings.llm_base_url)
    if (
        url.scheme != "https"
        or url.hostname != OPENCODE_HOST
        or settings.llm_model != OPENCODE_MODEL
        or not settings.llm_api_key
        or not settings.llm_opencode_session
    ):
        raise ValueError("Live evaluation requires the configured OpenCode Go profile.")


async def run(
    dataset_path: Path,
    output: Path,
    settings: Settings,
    *,
    live: bool,
    case_ids: set[str],
    full_catalog: bool = False,
) -> None:
    raw = await asyncio.to_thread(dataset_path.read_bytes)
    dataset = Dataset.model_validate_json(raw)
    unknown = case_ids - {case.id for case in dataset.cases}
    if unknown:
        raise ValueError(f"Unknown case IDs: {', '.join(sorted(unknown))}")

    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=True)
    cases = [case for case in dataset.cases if not case_ids or case.id in case_ids]
    if live:
        validate_live_profile(settings)
    if full_catalog and settings.postgres_host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Full MCP discovery requires local PostgreSQL.")
    stdio_settings = replace(settings, mcp_transport=MCPTransport.STDIO)
    clients = create_tool_clients(stdio_settings) if full_catalog else None
    mcp = clients.math if clients else create_math_tools_client(stdio_settings)
    if mcp is None:
        raise RuntimeError("MathMCP stdio initialization failed.")

    report: dict[str, object] = {
        "dataset_sha256": hashlib.sha256(raw).hexdigest(),
        "queries_sha256": hashlib.sha256(
            json.dumps([(case.id, case.query) for case in cases], ensure_ascii=False).encode()
        ).hexdigest(),
        "provider": "OpenCode Go" if live else None,
        "model": settings.llm_model if live else None,
        "temperature": 0.0,
        "window": settings.default_context_length,
        "output_tokens": settings.llm_max_tokens,
        "generation_reserve_tokens": settings.llm_generation_reserve_tokens,
        "llm_timeout_seconds": settings.llm_timeout,
        "agent_timeout_seconds": settings.agent_timeout,
        "max_llm_steps": settings.agent_max_llm_steps,
        "max_tool_calls": settings.agent_max_tool_calls,
        "catalog": "full" if full_catalog else "math",
    }
    results: list[dict[str, object]] = []
    report["cases"] = results

    async with AsyncExitStack() as stack:
        started = monotonic()
        agent_mcp: ToolClient = mcp
        if clients is None:
            stack.push_async_callback(mcp.aclose)
            if not await mcp.start():
                raise RuntimeError("MathMCP discovery failed.")
        else:
            stack.push_async_callback(clients.composite.aclose)
            await clients.composite.start()
            if not clients.student.available or not mcp.available:
                raise RuntimeError("Full MCP discovery requires MathMCP and a local PostgreSQL.")
            agent_mcp = clients.composite
        math_names = frozenset(mcp.tools)
        report.update(discovery_ms=elapsed_ms(started), tools=sorted(agent_mcp.tools))
        llm = (
            RecordingLLM(await stack.enter_async_context(OpenAIClient(settings))) if live else None
        )
        budget = ContextBudget(
            settings.default_context_length,
            settings.llm_max_tokens,
            settings.llm_generation_reserve_tokens,
        )
        for case in cases:
            row: dict[str, object] = {
                "id": case.id,
                "reference": case.reference,
                "image_expected": case.image,
                "mandatory": case.mandatory,
                "direct": await direct_case(mcp, case, output),
            }
            if llm is not None:
                row["live"] = await live_case(
                    agent_mcp, llm, settings, case, output, budget, math_names
                )
            results.append(row)
            await asyncio.to_thread(
                (output / "report.json").write_text,
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            print(case.id, "recorded", flush=True)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Evaluate MathMCP through the production client.")
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--full-catalog", action="store_true", help="Requires local PostgreSQL.")
    parser.add_argument("--window", type=int, default=131072)
    parser.add_argument("--output-tokens", type=int, default=32768)
    args = parser.parse_args(argv)
    if args.window < 512 or not 64 <= args.output_tokens <= args.window // 2:
        parser.error("Require window >= 512 and 64 <= output tokens <= window / 2.")
    if args.full_catalog and not args.live:
        parser.error("--full-catalog applies to live evaluation.")

    if args.live:
        settings = Settings.load(args.env_file)
    else:
        settings = Settings(bot_token="", postgres_password="")
    settings = replace(
        settings, default_context_length=args.window, llm_max_tokens=args.output_tokens
    )
    asyncio.run(
        run(
            args.dataset,
            args.output,
            settings,
            live=args.live,
            case_ids=set(args.case),
            full_catalog=args.full_catalog,
        )
    )


if __name__ == "__main__":
    main()
