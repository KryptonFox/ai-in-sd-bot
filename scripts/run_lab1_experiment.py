import asyncio
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings
from app.core.schemas.llm import LLMRequest, LLMResponse
from app.core.schemas.message import ChatMessage, MessageRole
from app.llm.client import OpenAIClient
from app.logging_setup import configure_logging
from app.modes.definitions import STUDY_MODE

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("experiment")

QUERY = (
    "Объясни разницу между процессами и потоками в ОС, и в каких случаях в "
    "Python использовать multiprocessing вместо threading"
)
TEMPERATURES = [0.0, 0.3, 0.7, 1.0]
RUNS_PER_TEMP = 3


async def run_experiment() -> dict[str, object]:
    settings = Settings.load()
    configure_logging(settings)

    async with OpenAIClient(settings) as client:
        return await _run_experiment(settings, client)


async def _run_experiment(
    settings: Settings,
    client: OpenAIClient,
) -> dict[str, object]:
    logger.info("Starting Lab 1 experiment using model: %s", settings.llm_model)
    results = await _temperature_runs(settings, client)
    comparison = await _compare_prompts(settings, client)

    return {
        "model": settings.llm_model,
        "query": QUERY,
        "date": "2026-10-06",
        "runs": results,
        "comparison": comparison,
    }


async def _temperature_runs(
    settings: Settings,
    client: OpenAIClient,
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []

    for temperature in TEMPERATURES:
        for repeat in range(1, RUNS_PER_TEMP + 1):
            run_index = len(results) + 1
            logger.info(
                "Run #%d: temp=%.1f (repeat %d/3)...",
                run_index,
                temperature,
                repeat,
            )
            request = LLMRequest(
                messages=[
                    ChatMessage(role=MessageRole.SYSTEM, content=STUDY_MODE.system_prompt),
                    ChatMessage(role=MessageRole.USER, content=QUERY),
                ],
                model=settings.llm_model,
                temperature=temperature,
                max_tokens=1500,
            )
            response, elapsed = await _run_with_retries(
                client,
                request,
                run_index,
            )
            item: dict[str, object] = {
                "run_idx": run_index,
                "temperature": temperature,
                "repeat": repeat,
            }
            if response is None:
                item["error"] = "Failed after retries"
            else:
                item["elapsed_sec"] = round(elapsed, 2)
                item.update(_response_metrics(response, temperature))
                logger.info(
                    "Run #%d OK in %.2fs (%d chars, tokens: %s)",
                    run_index,
                    elapsed,
                    len(response.content),
                    item["usage"],
                )

            results.append(item)
            await asyncio.sleep(2)

    return results


async def _run_with_retries(
    client: OpenAIClient, request: LLMRequest, run_index: int
) -> tuple[LLMResponse | None, float]:
    for attempt in range(1, 3):
        started = time.monotonic()

        try:
            response = await client.generate(request)
            return response, time.monotonic() - started
        except Exception:
            logger.warning("Run #%d attempt %d failed", run_index, attempt)
            if attempt == 2:
                return None, time.monotonic() - started

            await asyncio.sleep(3)

    raise RuntimeError("experiment_attempts_exhausted")


def _response_metrics(
    response: LLMResponse,
    temperature: float,
) -> dict[str, object]:
    content = response.content
    lowered = content.lower()
    has_code = "```" in content
    has_summary = any(word in lowered for word in ("разниц", "процесс", "поток", "памят", "gil"))
    has_recommendations = any(
        word in lowered for word in ("вывод", "итог", "рекомендац", "когда использова", "резюме")
    )
    observations: list[str] = []
    if "GIL" in content or "gil" in content:
        observations.append("Упомянут GIL")

    if "multiprocessing" in content and "threading" in content:
        observations.append("Охвачены обе библиотеки")

    if has_code:
        observations.append("Есть пример кода")

    if temperature == 0.0:
        observations.append("Детерминированная структура")
    elif temperature >= 0.7:
        observations.append("Повышенная вариативность формулировок")

    return {
        "char_count": len(content),
        "format_ok": has_code and has_summary and has_recommendations,
        "usage": {
            "prompt_tokens": response.prompt_tokens,
            "completion_tokens": response.completion_tokens,
            "total_tokens": response.total_tokens,
        },
        "observations": ", ".join(observations),
        "snippet": content[:300].replace("\n", " "),
        "full_content": content,
    }


async def _compare_prompts(
    settings: Settings,
    client: OpenAIClient,
) -> dict[str, object]:
    logger.info("Running Prompt Comparison...")
    prompts = {
        "bare": "Ты помощник по программированию. Отвечай на вопросы пользователя.",
        "rctf": STUDY_MODE.system_prompt,
    }
    results: dict[str, object] = {}

    for name, prompt in prompts.items():
        request = LLMRequest(
            messages=[
                ChatMessage(role=MessageRole.SYSTEM, content=prompt),
                ChatMessage(role=MessageRole.USER, content=QUERY),
            ],
            model=settings.llm_model,
            temperature=0.3,
            max_tokens=1500,
        )
        started = time.monotonic()
        response = await client.generate(request)
        results[name] = {
            "elapsed_sec": round(time.monotonic() - started, 2),
            "char_count": len(response.content),
            "usage": {
                "prompt_tokens": response.prompt_tokens,
                "completion_tokens": response.completion_tokens,
                "total_tokens": response.total_tokens,
            },
            "content": response.content,
        }
        await asyncio.sleep(2)

    return results


def main() -> None:
    output = asyncio.run(run_experiment())
    out_file = Path(__file__).resolve().parent / "experiment_results.json"

    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    logger.info("Saved experiment results to %s", out_file)


if __name__ == "__main__":
    main()
