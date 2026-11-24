import asyncio
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from mcp_servers.math_tools.errors import MathError
from mcp_servers.math_tools.limits import OPERATION_TIMEOUT_SECONDS

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKER_MODULE = "mcp_servers.math_tools.worker"
STARTUP_TIMEOUT_SECONDS = 30.0
WORKER_STREAM_LIMIT_BYTES = 4 * 1024 * 1024
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


class MathWorker:
    def __init__(
        self,
        *,
        command: Sequence[str] | None = None,
        timeout: float = OPERATION_TIMEOUT_SECONDS,
        startup_timeout: float = STARTUP_TIMEOUT_SECONDS,
    ) -> None:
        self._command = list(command) if command is not None else None
        self._timeout = timeout
        self._startup_timeout = startup_timeout
        self._lock = asyncio.Lock()
        self._process: asyncio.subprocess.Process | None = None

    @property
    def pid(self) -> int | None:
        process = self._process
        return process.pid if process is not None else None

    @property
    def is_running(self) -> bool:
        process = self._process
        return process is not None and process.returncode is None

    async def request(self, payload: Mapping[str, object]) -> dict[str, object]:
        async with self._lock:
            process = await self._ensure_process()
            try:
                return await asyncio.wait_for(
                    self._exchange(process, payload),
                    timeout=self._timeout,
                )
            except TimeoutError:
                await self._kill(process)
                raise MathError("timeout", "Вычисление не уложилось в 5 секунд.") from None
            except asyncio.CancelledError:
                await self._kill(process)
                raise
            except _WorkerTransportError:
                await self._kill(process)
                raise MathError("worker_failed") from None

    async def aclose(self) -> None:
        async with self._lock:
            process = self._process
            if process is None:
                return
            await self._kill(process)

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
                env=_worker_environment(),
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
            raise MathError("timeout", "Вычислительный процесс не запустился вовремя.") from None
        except asyncio.CancelledError:
            await self._kill(process)
            raise
        except ValueError:
            await self._kill(process)
            raise MathError("worker_failed") from None

        try:
            ready = json.loads(ready_line)
        except (UnicodeDecodeError, json.JSONDecodeError):
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

        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        try:
            process.stdin.write(encoded + b"\n")
            await process.stdin.drain()
            raw_response = await process.stdout.readline()
        except (BrokenPipeError, ConnectionResetError, ValueError):
            raise _WorkerTransportError from None

        if not raw_response:
            raise _WorkerTransportError

        try:
            response = json.loads(raw_response)
        except (UnicodeDecodeError, json.JSONDecodeError):
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

    async def _kill(self, process: asyncio.subprocess.Process) -> None:
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


def _worker_environment() -> dict[str, str]:
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
        }
    )
    return environment


class MathRuntime:
    def __init__(self, worker: MathWorker | None = None) -> None:
        self.worker = worker or MathWorker()

    async def request(self, payload: Mapping[str, object]) -> dict[str, object]:
        return await self.worker.request(payload)

    async def aclose(self) -> None:
        await self.worker.aclose()
