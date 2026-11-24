import asyncio
import os
import sys
import time
from typing import cast

import pytest

from mcp_servers.math_tools.errors import MathError
from mcp_servers.math_tools.limits import MEMORY_LIMIT_BYTES, OPERATION_TIMEOUT_SECONDS
from mcp_servers.math_tools.runtime import MathRuntime, MathWorker

SLEEPING_WORKER = """
import json
import sys
import time

print(json.dumps({"ready": True}), flush=True)
time.sleep(60)
"""

ECHO_WORKER = """
import json
import sys
import time

print(json.dumps({"ready": True}), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    if request.get("operation") != "warmup":
        time.sleep(0.2)
    response = {"ok": True, "result": {"value": request.get("value")}}
    print(json.dumps(response), flush=True)
"""

STARTUP_STALL_WORKER = """
import time
time.sleep(60)
"""

CRASHING_WORKER = """
import json
import sys

print(json.dumps({"ready": True}), flush=True)
sys.exit(1)
"""

LARGE_PAYLOAD_WORKER = """
import json
import sys

print(json.dumps({"ready": True}), flush=True)
for line in sys.stdin:
    response = {"ok": True, "result": {"payload": "x" * 300000}}
    print(json.dumps(response), flush=True)
"""


def python_command(source: str) -> list[str]:
    return [sys.executable, "-c", source]


async def wait_for_process(worker: MathWorker) -> int:
    for _ in range(300):
        pid = worker.pid
        if pid is not None:
            return pid
        await asyncio.sleep(0.01)
    raise AssertionError("worker process did not start")


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def test_real_worker_reports_limits_and_is_reaped() -> None:
    runtime = MathRuntime()
    try:
        limits = await runtime.request({"operation": "limits"})
        assert limits["memory_limit_bytes"] == MEMORY_LIMIT_BYTES
        assert limits["timeout_seconds"] == OPERATION_TIMEOUT_SECONDS

        result = await runtime.request(
            {
                "operation": "calculate",
                "calculation": "numeric",
                "expression": "sqrt(2)",
                "variable": "x",
            }
        )
        assert result["status"] == "ok"
        assert str(result["result"]).startswith("1.4142135623731")
        assert runtime.worker.is_running
        pid = runtime.worker.pid
        assert pid is not None
    finally:
        await runtime.aclose()

    assert runtime.worker.is_running is False
    assert runtime.worker.pid is None
    assert not process_exists(pid)


async def test_timeout_kills_and_joins_worker_process() -> None:
    worker = MathWorker(command=python_command(SLEEPING_WORKER), timeout=0.3)
    task = asyncio.create_task(worker.request({"operation": "calculate"}))
    pid = await wait_for_process(worker)

    with pytest.raises(MathError) as captured:
        await task
    assert captured.value.code == "timeout"
    assert worker.is_running is False
    assert worker.pid is None
    assert not process_exists(pid)


async def test_startup_timeout_has_distinct_code_and_metrics() -> None:
    worker = MathWorker(
        command=python_command(STARTUP_STALL_WORKER),
        startup_timeout=0.2,
    )

    with pytest.raises(MathError) as captured:
        await worker.request({"operation": "calculate"})

    assert captured.value.code == "worker_start_timeout"
    metrics = worker.last_metrics
    assert metrics is not None
    assert metrics.startup_seconds >= 0.15
    assert metrics.operation_seconds == 0


async def test_queue_timeout_has_distinct_code_and_measures_wait() -> None:
    worker = MathWorker(
        command=python_command(ECHO_WORKER),
        timeout=5,
        queue_timeout=0.05,
    )
    first = asyncio.create_task(worker.request({"operation": "calculate", "value": 1}))
    await wait_for_process(worker)
    await asyncio.sleep(0.02)

    try:
        with pytest.raises(MathError) as captured:
            await worker.request({"operation": "calculate", "value": 2})
        queue_metrics = worker.last_metrics
    finally:
        await first
        await worker.aclose()

    assert captured.value.code == "queue_timeout"
    assert queue_metrics is not None
    assert queue_metrics.queue_wait_seconds >= 0.04


async def test_warmup_starts_worker_and_reports_numeric_metrics() -> None:
    runtime = MathRuntime()
    try:
        result = await runtime.warmup()
        metrics = runtime.worker.last_metrics
    finally:
        await runtime.aclose()

    assert result["status"] == "ok"
    assert metrics is not None
    assert metrics.startup_seconds > 0
    assert metrics.operation_seconds > 0
    assert metrics.queue_wait_seconds >= 0


async def test_cancellation_kills_and_joins_worker_process() -> None:
    worker = MathWorker(command=python_command(SLEEPING_WORKER), timeout=30)
    task = asyncio.create_task(worker.request({"operation": "calculate"}))
    pid = await wait_for_process(worker)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert worker.is_running is False
    assert worker.pid is None
    assert not process_exists(pid)


async def test_requests_are_serialized_through_one_worker() -> None:
    worker = MathWorker(command=python_command(ECHO_WORKER), timeout=5)
    try:
        await worker.request({"operation": "warmup"})
        started = time.monotonic()
        first, second = await asyncio.gather(
            worker.request({"operation": "calculate", "value": 1}),
            worker.request({"operation": "calculate", "value": 2}),
        )
        elapsed = time.monotonic() - started
    finally:
        await worker.aclose()

    assert first["value"] == 1
    assert second["value"] == 2
    assert elapsed >= 0.32


async def test_crashed_worker_reports_failure_and_cleans_up() -> None:
    worker = MathWorker(command=python_command(CRASHING_WORKER), timeout=1)
    with pytest.raises(MathError) as captured:
        await worker.request({"operation": "calculate"})
    assert captured.value.code == "worker_failed"
    assert worker.is_running is False

    replacement = MathWorker(command=python_command(ECHO_WORKER), timeout=5)
    try:
        result = await replacement.request({"operation": "warmup", "value": 7})
        assert result["value"] == 7
    finally:
        await replacement.aclose()


async def test_large_worker_response_exceeds_default_stream_limit() -> None:
    worker = MathWorker(command=python_command(LARGE_PAYLOAD_WORKER), timeout=5)
    try:
        result = await worker.request({"operation": "plot"})
        assert len(str(result["payload"])) == 300000
    finally:
        await worker.aclose()


async def test_kill_waits_for_reaped_process_until_wait_returns() -> None:
    class FakeProcess:
        returncode: int | None = None
        stdin = None

        def __init__(self) -> None:
            self.killed = False
            self.wait_started = asyncio.Event()
            self.wait_finished = asyncio.Event()
            self.release_wait = asyncio.Event()

        def kill(self) -> None:
            self.killed = True

        async def wait(self) -> int:
            self.wait_started.set()
            await self.release_wait.wait()
            self.wait_finished.set()
            self.returncode = -9
            return -9

    worker = MathWorker()
    process = FakeProcess()
    worker._process = cast(asyncio.subprocess.Process, process)

    kill_task = asyncio.create_task(worker._kill(worker._process))
    await process.wait_started.wait()
    await asyncio.sleep(0.05)
    assert not kill_task.done()
    assert process.killed
    assert not process.wait_finished.is_set()

    process.release_wait.set()
    await kill_task
    assert process.wait_finished.is_set()
    assert worker.pid is None
