import asyncio
import logging
import os
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from mcp_servers.math_tools.errors import MathError
from mcp_servers.math_tools.ipc import IpcProtocolError, decode_message, encode_message
from mcp_servers.math_tools.limits import MEMORY_LIMIT_MIB, OPERATION_TIMEOUT_SECONDS
from mcp_servers.math_tools.operations import WorkerOperation

logger = logging.getLogger("math_tools.runtime")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKER_MODULE = "mcp_servers.math_tools.worker"
DEFAULT_STARTUP_TIMEOUT_SECONDS = 30.0
DEFAULT_QUEUE_TIMEOUT_SECONDS = 5.0
WORKER_STREAM_LIMIT_BYTES = 4 * 1024 * 1024
SAFE_OPERATIONS = frozenset(operation.value for operation in WorkerOperation)
SENSITIVE_ENVIRONMENT_KEYS = {
    "BOT_TOKEN",
    "DATABASE_URL",
    "LLM_API_KEY",
    "MCP_SHARED_SECRET",
    "OPENAI_API_KEY",
    "POSTGRES_PASSWORD",
    "TELEGRAM_PROXY_URL",
}


class _WorkerTransportError(Exception):
    pass


@dataclass(frozen=True)
class MathRequestMetrics:
    queue_wait_seconds: float
    startup_seconds: float
    operation_seconds: float


class MathWorker:
    def __init__(
        self,
        *,
        command: Sequence[str] | None = None,
        timeout: float = OPERATION_TIMEOUT_SECONDS,
        startup_timeout: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
        queue_timeout: float = DEFAULT_QUEUE_TIMEOUT_SECONDS,
        memory_limit_mib: int = MEMORY_LIMIT_MIB,
    ) -> None:
        self._command = list(command) if command is not None else None
        self._timeout = timeout
        self._startup_timeout = startup_timeout
        self._queue_timeout = queue_timeout
        self._memory_limit_mib = memory_limit_mib
        self._lock = asyncio.Lock()
        self._process: asyncio.subprocess.Process | None = None
        self.last_metrics: MathRequestMetrics | None = None

    @property
    def pid(self) -> int | None:
        process = self._process
        return process.pid if process is not None else None

    @property
    def is_running(self) -> bool:
        process = self._process
        return process is not None and process.returncode is None

    async def warmup(self) -> dict[str, object]:
        return await self.request({"operation": WorkerOperation.WARMUP.value})

    async def request(
        self,
        payload: Mapping[str, object],
        *,
        operation_timeout: float | None = None,
    ) -> dict[str, object]:
        operation = str(payload.get("operation", ""))
        if operation not in SAFE_OPERATIONS:
            operation = "unknown"
        started = time.monotonic()
        code = "ok"
        queue_wait_seconds = 0.0
        startup_started: float | None = None
        startup_seconds = 0.0
        operation_started: float | None = None
        operation_seconds = 0.0
        process: asyncio.subprocess.Process | None = None

        try:
            async with asyncio.timeout(self._queue_timeout):
                await self._lock.acquire()
        except TimeoutError:
            queue_wait_seconds = time.monotonic() - started
            self._record_metrics(
                operation,
                started,
                MathRequestMetrics(queue_wait_seconds, 0.0, 0.0),
                "queue_timeout",
            )
            raise MathError("queue_timeout") from None

        try:
            queue_wait_seconds = time.monotonic() - started
            startup_started = time.monotonic()
            process = await self._ensure_process()
            startup_seconds = time.monotonic() - startup_started
            operation_started = time.monotonic()
            result = await asyncio.wait_for(
                self._exchange(process, payload),
                timeout=self._timeout if operation_timeout is None else operation_timeout,
            )
            operation_seconds = time.monotonic() - operation_started
            return result
        except TimeoutError:
            code = "timeout"
            await self._kill(process)
            raise MathError("timeout") from None
        except asyncio.CancelledError:
            code = "cancelled"
            await self._kill(process)
            raise
        except _WorkerTransportError:
            code = "worker_failed"
            await self._kill(process)
            raise MathError("worker_failed") from None
        except MathError as error:
            code = error.code
            raise
        finally:
            self._lock.release()
            if startup_started is not None and startup_seconds == 0.0:
                startup_seconds = time.monotonic() - startup_started
            if operation_started is not None and operation_seconds == 0.0:
                operation_seconds = time.monotonic() - operation_started
            self._record_metrics(
                operation,
                started,
                MathRequestMetrics(
                    queue_wait_seconds=max(0.0, queue_wait_seconds),
                    startup_seconds=startup_seconds,
                    operation_seconds=operation_seconds,
                ),
                code,
            )

    async def aclose(self) -> None:
        async with self._lock:
            process = self._process
            if process is None:
                return
            await self._kill(process)

    def _record_metrics(
        self,
        operation: str,
        started: float,
        metrics: MathRequestMetrics,
        code: str,
    ) -> None:
        self.last_metrics = metrics
        logger.info(
            "math_request contract=worker operation=%s total_ms=%.0f queue_ms=%.0f "
            "startup_ms=%.0f operation_ms=%.0f code=%s",
            operation or "unknown",
            (time.monotonic() - started) * 1000,
            metrics.queue_wait_seconds * 1000,
            metrics.startup_seconds * 1000,
            metrics.operation_seconds * 1000,
            code,
        )

    async def _ensure_process(self) -> asyncio.subprocess.Process:
        process = self._process
        if process is not None and process.returncode is None:
            return process
        if process is not None:
            await self._kill(process)

        command = self._command or [sys.executable, "-m", WORKER_MODULE]
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=str(PROJECT_ROOT),
                env=_worker_environment(self._memory_limit_mib, self._timeout),
                limit=WORKER_STREAM_LIMIT_BYTES,
            )
        except (OSError, ValueError):
            raise MathError("worker_failed") from None

        self._process = process
        try:
            ready_line = await asyncio.wait_for(
                process.stdout.readline() if process.stdout is not None else _empty_reader(),
                timeout=self._startup_timeout,
            )
        except TimeoutError:
            await self._kill(process)
            raise MathError("worker_start_timeout") from None
        except asyncio.CancelledError:
            await self._kill(process)
            raise
        except ValueError:
            await self._kill(process)
            raise MathError("worker_failed") from None

        try:
            ready = decode_message(ready_line)
        except IpcProtocolError:
            await self._kill(process)
            raise MathError("worker_failed") from None
        if not isinstance(ready, dict) or ready.get("ready") is not True:
            await self._kill(process)
            raise MathError("worker_failed")
        return process

    async def _exchange(
        self,
        process: asyncio.subprocess.Process,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        if process.stdin is None or process.stdout is None:
            raise _WorkerTransportError

        encoded = encode_message(payload)
        try:
            process.stdin.write(encoded)
            await process.stdin.drain()
            raw_response = await process.stdout.readline()
        except (BrokenPipeError, ConnectionResetError, ValueError):
            raise _WorkerTransportError from None

        if not raw_response:
            raise _WorkerTransportError

        try:
            response = decode_message(raw_response)
        except IpcProtocolError:
            raise _WorkerTransportError from None
        if not isinstance(response, dict):
            raise _WorkerTransportError

        if response.get("ok") is True:
            result = response.get("result")
            if not isinstance(result, dict):
                raise _WorkerTransportError
            return result

        code = response.get("code")
        message = response.get("message")
        if not isinstance(code, str) or not isinstance(message, str):
            raise _WorkerTransportError
        raise MathError(code, message)

    async def _kill(self, process: asyncio.subprocess.Process | None) -> None:
        if process is None:
            return
        if process.stdin is not None and not process.stdin.is_closing():
            process.stdin.close()
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        try:
            await process.wait()
        finally:
            if self._process is process:
                self._process = None


async def _empty_reader() -> bytes:
    return b""


def _worker_environment(memory_limit_mib: int, operation_timeout: float) -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items() if key not in SENSITIVE_ENVIRONMENT_KEYS
    }
    python_path = environment.get("PYTHONPATH")
    project_root = str(PROJECT_ROOT)
    environment["PYTHONPATH"] = (
        project_root if not python_path else project_root + os.pathsep + python_path
    )
    environment.update(
        {
            "MPLBACKEND": "Agg",
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "NUMEXPR_NUM_THREADS": "1",
            "MATH_MCP_MEMORY_LIMIT_MIB": str(memory_limit_mib),
            "MATH_MCP_OPERATION_TIMEOUT_SECONDS": str(operation_timeout),
        }
    )
    return environment


class MathRuntime:
    def __init__(
        self,
        worker: MathWorker | None = None,
        *,
        startup_timeout: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
        operation_timeout: float = OPERATION_TIMEOUT_SECONDS,
        queue_timeout: float = DEFAULT_QUEUE_TIMEOUT_SECONDS,
        memory_limit_mib: int = MEMORY_LIMIT_MIB,
    ) -> None:
        self.worker = worker or MathWorker(
            timeout=operation_timeout,
            startup_timeout=startup_timeout,
            queue_timeout=queue_timeout,
            memory_limit_mib=memory_limit_mib,
        )

    async def warmup(self) -> dict[str, object]:
        return await self.worker.warmup()

    async def request(self, payload: Mapping[str, object]) -> dict[str, object]:
        return await self.worker.request(payload)

    async def aclose(self) -> None:
        await self.worker.aclose()
