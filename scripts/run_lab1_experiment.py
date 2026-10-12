import asyncio
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings
from app.core.schemas.llm import LLMRequest
from app.core.schemas.message import ChatMessage, MessageRole
from app.llm.client import OpenAIClient
from app.modes.definitions import STUDY_MODE

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("experiment")

QUERY = (
    "Объясни разницу между процессами и потоками в ОС, и в каких случаях в "
    "Python использовать multiprocessing вместо threading"
)
TEMPERATURES = [0.0, 0.3, 0.7, 1.0]
RUNS_PER_TEMP = 3


async def run_experiment() -> dict:
    settings = Settings.load()
    client = OpenAIClient(settings)

    logger.info("Starting Lab 1 experiment using model: %s", settings.llm_model)
    results = []

    run_idx = 1
    for temp in TEMPERATURES:
        for repeat in range(1, RUNS_PER_TEMP + 1):
            logger.info("Run #%d: temp=%.1f (repeat %d/3)...", run_idx, temp, repeat)
            messages = [
                ChatMessage(role=MessageRole.SYSTEM, content=STUDY_MODE.system_prompt),
                ChatMessage(role=MessageRole.USER, content=QUERY),
            ]
            req = LLMRequest(
                messages=messages,
                model=settings.llm_model,
                temperature=temp,
                max_tokens=1500,
            )

            resp = None
            for attempt in range(1, 3):
                start = time.monotonic()
                try:
                    resp = await client.generate(req)
                    elapsed = time.monotonic() - start
                    break
                except Exception as e:
                    logger.warning("Run #%d attempt %d failed: %s", run_idx, attempt, e)
                    if attempt < 2:
                        await asyncio.sleep(3)
                    else:
                        resp = None
                        elapsed = time.monotonic() - start

            if resp is not None:
                content = resp.content
                char_count = len(content)
                usage = {
                    "prompt_tokens": resp.prompt_tokens,
                    "completion_tokens": resp.completion_tokens,
                    "total_tokens": resp.total_tokens,
                }

                has_code = "```" in content
                has_summary = any(
                    k in content.lower() for k in ["разниц", "процесс", "поток", "памят", "gil"]
                )
                has_recs = any(
                    k in content.lower()
                    for k in [
                        "вывод",
                        "итог",
                        "рекомендац",
                        "когда использова",
                        "резюме",
                    ]
                )
                format_ok = has_code and has_summary and has_recs

                obs = []
                if "GIL" in content or "gil" in content:
                    obs.append("Упомянут GIL")
                if "multiprocessing" in content and "threading" in content:
                    obs.append("Охвачены обе библиотеки")
                if has_code:
                    obs.append("Есть пример кода")
                if temp == 0.0:
                    obs.append("Детерминированная структура")
                elif temp >= 0.7:
                    obs.append("Повышенная вариативность формулировок")

                item = {
                    "run_idx": run_idx,
                    "temperature": temp,
                    "repeat": repeat,
                    "elapsed_sec": round(elapsed, 2),
                    "char_count": char_count,
                    "format_ok": format_ok,
                    "usage": usage,
                    "observations": ", ".join(obs),
                    "snippet": content[:300].replace("\n", " "),
                    "full_content": content,
                }
                results.append(item)
                logger.info(
                    "Run #%d OK in %.2fs (%d chars, tokens: %s)",
                    run_idx,
                    elapsed,
                    char_count,
                    usage,
                )
            else:
                results.append(
                    {
                        "run_idx": run_idx,
                        "temperature": temp,
                        "repeat": repeat,
                        "error": "Failed after retries",
                    }
                )

            run_idx += 1
            await asyncio.sleep(2)

    logger.info("Running Prompt Comparison...")
    bare_prompt = "Ты помощник по программированию. Отвечай на вопросы пользователя."
    rctf_prompt = STUDY_MODE.system_prompt

    comp_results = {}
    for name, p in [("bare", bare_prompt), ("rctf", rctf_prompt)]:
        req = LLMRequest(
            messages=[
                ChatMessage(role=MessageRole.SYSTEM, content=p),
                ChatMessage(role=MessageRole.USER, content=QUERY),
            ],
            model=settings.llm_model,
            temperature=0.3,
            max_tokens=1500,
        )
        start = time.monotonic()
        resp = await client.generate(req)
        elapsed = time.monotonic() - start

        comp_results[name] = {
            "elapsed_sec": round(elapsed, 2),
            "char_count": len(resp.content),
            "usage": {
                "prompt_tokens": resp.prompt_tokens,
                "completion_tokens": resp.completion_tokens,
                "total_tokens": resp.total_tokens,
            },
            "content": resp.content,
        }
        await asyncio.sleep(2)

    return {
        "model": settings.llm_model,
        "query": QUERY,
        "date": "2026-10-06",
        "runs": results,
        "comparison": comp_results,
    }


def main() -> None:
    output = asyncio.run(run_experiment())
    out_file = Path(__file__).resolve().parent / "experiment_results.json"

    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    logger.info("Saved experiment results to %s", out_file)


if __name__ == "__main__":
    main()
