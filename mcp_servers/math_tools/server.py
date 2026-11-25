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
from mcp_servers.math_tools.limits import (
    MAX_EQUATIONS,
    MAX_EXPRESSION_LENGTH,
    MAX_FUNCTIONS,
    MAX_PLOT_LEVELS,
    MAX_VARIABLES,
)
from mcp_servers.math_tools.schemas import (
    CalculateOperation,
    LimitDirection,
    MathCalculateResult,
    MathPlotResult,
    MathSolveResult,
    PlotOutput,
    PlotType,
)
from mcp_servers.math_tools.service import MathService
from mcp_servers.tool_errors import raise_tool_error

CALCULATE_DESCRIPTION = (
    "Математические вычисления с проверкой выражения. "
    "old operations: simplify, evaluate (точное значение), numeric (15 значащих цифр), "
    "diff (производная по одной переменной variable, включая частную; order=1..5, "
    "например вторая производная по x — diff, variable=x, order=2), "
    "integrate (lower/upper; lower=0, upper=oo — несобственный интеграл), "
    "limit (point и direction both/left/right). "
    "new operations: partial_diff (независимые частные производные по РАЗЛИЧНЫМ "
    "переменным из variables, не последовательное дифференцирование), "
    "jacobian (functions — список функций, variables — список переменных), "
    "constrained_extrema (expression — исходная функция, variables — переменные, "
    "constraints — одно исходное уравнение-ограничение; множитель Лагранжа сервер "
    "добавляет сам, не записывай lambda в уравнение), "
    "series (expression, variable=n, lower/upper — границы суммирования, по умолчанию 1..oo). "
    "Для integrate и series возвращается convergence: convergent/divergent/unknown. "
    "Примеры: integrate log(1+x^2)/(1+x^2) x=0..oo; limit (x^2-1)/(x-1) в точке 1; "
    "series 1/n^2 по n; jacobian functions=[x^2*y, x+y], variables=[x,y]. "
    "Степень записывай через ** или ^, умножение указывай явно (*). "
    "Доступны sin, cos, tan, asin, acos, atan, sinh, cosh, tanh, sqrt, exp, log, ln, abs, "
    "floor, ceil, sign, factorial, gamma, min, max. Выражение до 512 символов и 128 узлов."
)

SOLVE_DESCRIPTION = (
    "Решает уравнение или систему до трёх уравнений. "
    "Уравнение можно записать как x^2 = 4 или как выражение, равное нулю. "
    "variables перечисляет все неизвестные; если не указаны, они определяются по уравнениям. "
    "Имена переменных — идентификаторы без ключевых слов Python; для ручного метода "
    "Лагранжа используй lam или lmbda, а не lambda. "
    "По умолчанию возвращаются только вещественные решения. Каждое решение проверяется "
    "подстановкой в исходные уравнения и по области определения."
)

PLOT_DESCRIPTION = (
    "Строит график и возвращает PNG. plot_type=curve (по умолчанию) — до четырёх y=f(x), "
    "старые аргументы functions/variable/x_min/x_max сохраняются. "
    "plot_type=parametric — functions=[x(t), y(t)], parameter=t, x_min/x_max задают диапазон t. "
    "plot_type=contour — функция f(x,y), variable=x, y_variable=y, x/y диапазоны, "
    "необязательные levels (точные уровни). "
    "plot_type=surface — 3D z=f(x,y) с маской недопустимых значений. "
    "plot_type=domain_coloring — комплексная f(z), hue=arg, brightness=normalized |f(z)|, "
    "маски полюсов и разрезов. "
    "plot_type=complex_height — 3D |f(z)| над комплексной плоскостью; нули в области "
    "отмечаются маркерами и возвращаются в zero_markers. "
    "y_min/y_max по умолчанию равны x-диапазону. "
    "gamma и factorial доступны на вещественных графиках. "
    "В комплексном графике floor и ceil требуют вещественный аргумент, например abs(z). "
    "Примеры: parametric functions=[cos(t), sin(t)], x_min=0, x_max=6.283185307179586; "
    "surface functions=[x*y], x_min=-2, x_max=2, y_min=-2, y_max=2; "
    "domain_coloring functions=[1/z], variable=z, x_min=-2, x_max=2, y_min=-2, y_max=2."
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


VariableName = Annotated[str, Field(min_length=1, max_length=128)]
DerivativeOrder = Annotated[
    int,
    Field(
        ge=1,
        le=5,
        description=(
            "Порядок производной для operation=diff: 1 — первая, 2 — вторая и т.д., до 5."
        ),
    ),
]
CalculationVariables = Annotated[
    list[str],
    Field(
        max_length=MAX_VARIABLES,
        description=(
            "Различные переменные для partial_diff, jacobian и constrained_extrema. "
            "Для второй частной производной по одной переменной используйте "
            "operation=diff, variable=x, order=2."
        ),
    ),
]
JacobianFunctions = Annotated[
    list[ExpressionText],
    Field(
        max_length=MAX_FUNCTIONS,
        description="Функции для operation=jacobian.",
    ),
]
ExtremumConstraints = Annotated[
    list[EquationText],
    Field(
        max_length=MAX_EQUATIONS,
        description=(
            "Одно исходное уравнение-ограничение для operation=constrained_extrema. "
            "Множитель Лагранжа добавляется сервером; не записывай lambda."
        ),
    ),
]
SolveVariables = Annotated[
    list[str],
    Field(
        max_length=3,
        description=(
            "Неизвестные — идентификаторы без ключевых слов Python. "
            "Для ручного метода Лагранжа используй lam или lmbda, не lambda."
        ),
    ),
]
ContourLevels = Annotated[
    list[float],
    Field(
        max_length=MAX_PLOT_LEVELS,
        description="Точные уровни для plot_type=contour; можно один уровень.",
    ),
]


def _raise_tool_error(error: MathError) -> Never:
    raise_tool_error(error.code, error.message)


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

    read_only = {"readOnlyHint": True}
    tools = _MathTools(math_service)
    server.tool(
        tools.math_calculate,
        description=CALCULATE_DESCRIPTION,
        annotations=read_only,
    )
    server.tool(tools.math_solve, description=SOLVE_DESCRIPTION, annotations=read_only)
    server.tool(
        tools.math_plot,
        description=PLOT_DESCRIPTION,
        annotations=read_only,
        output_schema=MathPlotResult.model_json_schema(),
    )
    return server


class _MathTools:
    def __init__(self, service: MathService) -> None:
        self._service = service

    async def math_calculate(
        self,
        expression: ExpressionText | None = None,
        operation: CalculateOperation = CalculateOperation.SIMPLIFY,
        variable: VariableName | None = None,
        order: DerivativeOrder = 1,
        lower: str | float | int | None = None,
        upper: str | float | int | None = None,
        point: str | float | int | None = None,
        direction: LimitDirection = LimitDirection.BOTH,
        variables: CalculationVariables | None = None,
        functions: JacobianFunctions | None = None,
        constraints: ExtremumConstraints | None = None,
    ) -> MathCalculateResult:
        with _translate_tool_errors():
            return await self._service.calculate(
                expression,
                operation=operation,
                variable=variable,
                order=order,
                lower=lower,
                upper=upper,
                point=point,
                direction=direction,
                variables=variables,
                functions=functions,
                constraints=constraints,
            )

    async def math_solve(
        self,
        equations: Annotated[list[EquationText], Field(min_length=1, max_length=MAX_EQUATIONS)],
        variables: SolveVariables | None = None,
        real_only: bool = True,
    ) -> MathSolveResult:
        with _translate_tool_errors():
            return await self._service.solve(
                equations,
                variables=variables,
                real_only=real_only,
            )

    async def math_plot(
        self,
        functions: Annotated[
            list[ExpressionText],
            Field(min_length=1, max_length=MAX_FUNCTIONS),
        ],
        plot_type: PlotType = PlotType.CURVE,
        variable: VariableName | None = None,
        parameter: VariableName = "t",
        y_variable: VariableName = "y",
        x_min: float | None = None,
        x_max: float | None = None,
        y_min: float | None = None,
        y_max: float | None = None,
        levels: ContourLevels | None = None,
    ) -> ToolResult:
        with _translate_tool_errors():
            output = await self._service.plot(
                functions,
                plot_type=plot_type,
                variable=variable,
                parameter=parameter,
                y_variable=y_variable,
                x_min=x_min,
                x_max=x_max,
                y_min=y_min,
                y_max=y_max,
                levels=levels,
            )
            return _plot_tool_result(output)


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
