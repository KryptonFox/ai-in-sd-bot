import base64
import json
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager
from typing import Annotated, Never

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from mcp import types
from pydantic import Field, ValidationError

from mcp_servers.math_tools.errors import MathError
from mcp_servers.math_tools.limits import MAX_EQUATIONS, MAX_EXPRESSION_LENGTH, MAX_FUNCTIONS
from mcp_servers.math_tools.schemas import (
    CalculateOperation,
    LimitDirection,
    MathCalculateResult,
    MathPlotResult,
    MathSolveResult,
    PlotOutput,
)
from mcp_servers.math_tools.service import MathService

CALCULATE_DESCRIPTION = (
    "Математические вычисления с проверкой выражения. "
    "operation=simplify упрощает, evaluate даёт точное значение, numeric — 15 значащих цифр, "
    "diff — производную (order 1..5), integrate — неопределённый интеграл или определённый "
    "при lower и upper, limit — предел в точке point с direction both/left/right. "
    "Переменная по умолчанию x. Степень записывай через ** или ^, умножение указывай явно (*). "
    "Доступны sin, cos, tan, asin, acos, atan, sinh, cosh, tanh, sqrt, exp, log, ln, abs, "
    "floor, ceil, sign, factorial, gamma, min, max. Выражение до 512 символов и 128 узлов."
)

SOLVE_DESCRIPTION = (
    "Решает уравнение или систему до трёх уравнений. "
    "Уравнение можно записать как x^2 = 4 или как выражение, равное нулю. "
    "variables перечисляет все неизвестные; если не указаны, они определяются по уравнениям. "
    "По умолчанию возвращаются только вещественные решения. Каждое решение проверяется "
    "подстановкой в исходные уравнения и по области определения."
)

PLOT_DESCRIPTION = (
    "Строит от одной до четырёх функций y=f(x) на общих осях и возвращает PNG. "
    "Диапазон по умолчанию от -10 до 10. Кривые разрываются на полюсах, "
    "вне вещественной области и в известных точках разрыва."
)

ExpressionText = Annotated[
    str,
    Field(
        min_length=1,
        max_length=MAX_EXPRESSION_LENGTH,
        description="Математическое выражение, например x^2 + 2*x + 1.",
    ),
]
EquationText = Annotated[
    str,
    Field(
        min_length=1,
        max_length=MAX_EXPRESSION_LENGTH,
        description="Уравнение, например x^2 = 4 или x^2 - 4.",
    ),
]


def _raise_tool_error(error: MathError) -> Never:
    payload = {"error": {"code": error.code, "message": error.message}}
    raise ToolError(json.dumps(payload, ensure_ascii=False))


@contextmanager
def _translate_tool_errors() -> Generator[None, None, None]:
    try:
        yield
    except ToolError:
        raise
    except MathError as error:
        _raise_tool_error(error)
    except ValidationError:
        _raise_tool_error(MathError("invalid_arguments"))
    except (ValueError, TypeError):
        _raise_tool_error(MathError("invalid_arguments"))
    except Exception:
        _raise_tool_error(MathError("evaluation_failed"))


def build_server(service: MathService | None = None) -> FastMCP:
    math_service = service or MathService()

    @asynccontextmanager
    async def lifespan(_: FastMCP) -> AsyncGenerator[dict[str, object], None]:
        try:
            yield {}
        finally:
            await math_service.aclose()

    server = FastMCP(
        "math-tools",
        strict_input_validation=True,
        mask_error_details=True,
        lifespan=lifespan,
    )

    @server.tool(description=CALCULATE_DESCRIPTION, annotations={"readOnlyHint": True})
    async def math_calculate(
        expression: ExpressionText,
        operation: CalculateOperation = CalculateOperation.SIMPLIFY,
        variable: Annotated[str, Field(min_length=1, max_length=128)] = "x",
        order: Annotated[int, Field(ge=1, le=5)] = 1,
        lower: str | float | int | None = None,
        upper: str | float | int | None = None,
        point: str | float | int | None = None,
        direction: LimitDirection = LimitDirection.BOTH,
    ) -> MathCalculateResult:
        with _translate_tool_errors():
            return await math_service.calculate(
                expression,
                operation=operation,
                variable=variable,
                order=order,
                lower=lower,
                upper=upper,
                point=point,
                direction=direction,
            )

    @server.tool(description=SOLVE_DESCRIPTION, annotations={"readOnlyHint": True})
    async def math_solve(
        equations: Annotated[list[EquationText], Field(min_length=1, max_length=MAX_EQUATIONS)],
        variables: Annotated[list[str], Field(max_length=3)] | None = None,
        real_only: bool = True,
    ) -> MathSolveResult:
        with _translate_tool_errors():
            return await math_service.solve(
                equations,
                variables=variables,
                real_only=real_only,
            )

    @server.tool(
        description=PLOT_DESCRIPTION,
        annotations={"readOnlyHint": True},
        output_schema=MathPlotResult.model_json_schema(),
    )
    async def math_plot(
        functions: Annotated[
            list[ExpressionText],
            Field(min_length=1, max_length=MAX_FUNCTIONS),
        ],
        variable: Annotated[str, Field(min_length=1, max_length=128)] = "x",
        x_min: float = -10.0,
        x_max: float = 10.0,
    ) -> ToolResult:
        with _translate_tool_errors():
            output = await math_service.plot(
                functions,
                variable=variable,
                x_min=x_min,
                x_max=x_max,
            )
            return _plot_tool_result(output)

    return server


def _plot_tool_result(output: PlotOutput) -> ToolResult:
    payload = output.result.model_dump(mode="json")
    text = types.TextContent(
        type="text",
        text=json.dumps(payload, ensure_ascii=False),
    )
    image = types.ImageContent(
        type="image",
        data=base64.b64encode(output.png).decode("ascii"),
        mime_type="image/png",
    )
    return ToolResult(
        content=[text, image],
        structured_content=payload,
    )
