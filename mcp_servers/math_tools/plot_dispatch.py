import base64

from mcp_servers.math_tools.errors import MathError
from mcp_servers.math_tools.expressions import build_parsed_syntax, build_variable
from mcp_servers.math_tools.limits import PLOT_SAMPLES
from mcp_servers.math_tools.payloads import PlotRequest
from mcp_servers.math_tools.plotting import (
    PlotRendering,
    render_complex_height,
    render_contour,
    render_curve,
    render_domain_coloring,
    render_parametric,
    render_surface,
)
from mcp_servers.math_tools.schemas import (
    MathPlotResult,
    MathPlotZero,
    PlotStatus,
    PlotType,
)


def plot(request: PlotRequest) -> dict[str, object]:
    if request.plot_type is PlotType.CURVE:
        return _plot_curve(request)
    if request.plot_type is PlotType.PARAMETRIC:
        return _plot_parametric(request)
    if request.plot_type in (PlotType.CONTOUR, PlotType.SURFACE):
        return _plot_field(request)
    return _plot_complex(request)


def _plot_curve(request: PlotRequest) -> dict[str, object]:
    variable = request.variable
    parsed_functions = [
        (function.normalized, build_parsed_syntax(function)) for function in request.functions
    ]
    rendering = render_curve(
        parsed_functions,
        variable,
        build_variable(variable),
        request.x_min,
        request.x_max,
        PLOT_SAMPLES,
        max_png_bytes=request.max_png_bytes,
    )
    detail = (
        "График построен; разрывы и участки вне вещественной области разорваны."
        if rendering.has_points
        else "Ни одна функция не имеет вещественных значений на выбранном диапазоне."
    )
    return _plot_payload(
        plot_type=request.plot_type,
        rendering=rendering,
        variable=variable,
        x_min=request.x_min,
        x_max=request.x_max,
        y_min=None,
        y_max=None,
        detail=detail,
    )


def _plot_parametric(request: PlotRequest) -> dict[str, object]:
    parameter = request.parameter
    x_parsed = build_parsed_syntax(request.functions[0])
    y_parsed = build_parsed_syntax(request.functions[1])
    rendering = render_parametric(
        x_parsed,
        y_parsed,
        parameter,
        build_variable(parameter),
        request.x_min,
        request.x_max,
        PLOT_SAMPLES,
        max_png_bytes=request.max_png_bytes,
    )
    detail = (
        "Параметрическая кривая построена; разрывы области определения разорваны."
        if rendering.has_points
        else "Параметрическая кривая не имеет допустимых точек на выбранном диапазоне."
    )
    return _plot_payload(
        plot_type=request.plot_type,
        rendering=rendering,
        variable=parameter,
        x_min=request.x_min,
        x_max=request.x_max,
        y_min=None,
        y_max=None,
        detail=detail,
    )


def _plot_field(request: PlotRequest) -> dict[str, object]:
    variable = request.variable
    symbol = build_variable(variable)
    y_variable = request.y_variable
    y_symbol = build_variable(y_variable)
    y_min, y_max = _resolved_y_range(request)
    parsed = build_parsed_syntax(request.functions[0])

    if request.plot_type is PlotType.CONTOUR:
        rendering = render_contour(
            parsed,
            symbol,
            y_symbol,
            request.x_min,
            request.x_max,
            y_min,
            y_max,
            request.levels,
            max_png_bytes=request.max_png_bytes,
        )
        detail = (
            "Линии уровня построены; недопустимые значения и полюсы замаскированы."
            if rendering.has_points
            else "На выбранной области нет допустимых значений для линий уровня."
        )
    else:
        rendering = render_surface(
            parsed,
            symbol,
            y_symbol,
            request.x_min,
            request.x_max,
            y_min,
            y_max,
            max_png_bytes=request.max_png_bytes,
        )
        detail = (
            "Поверхность построена; недопустимые значения и полюсы замаскированы."
            if rendering.has_points
            else "На выбранной области нет допустимых значений для поверхности."
        )
    return _plot_payload(
        plot_type=request.plot_type,
        rendering=rendering,
        variable=variable,
        x_min=request.x_min,
        x_max=request.x_max,
        y_min=request.y_min,
        y_max=request.y_max,
        detail=detail,
    )


def _plot_complex(request: PlotRequest) -> dict[str, object]:
    variable = request.variable
    symbol = build_variable(variable)
    y_min, y_max = _resolved_y_range(request)
    parsed = build_parsed_syntax(request.functions[0])

    if request.plot_type is PlotType.DOMAIN_COLORING:
        rendering = render_domain_coloring(
            parsed,
            symbol,
            request.x_min,
            request.x_max,
            y_min,
            y_max,
            max_png_bytes=request.max_png_bytes,
        )
        detail = (
            "Цветовая карта аргумента построена; полюсы и разрезы замаскированы, "
            "яркость кодирует нормированный и ограниченный |f(z)|."
            if rendering.has_points
            else "На выбранной области нет конечных значений комплексной функции."
        )
    else:
        rendering = render_complex_height(
            parsed,
            symbol,
            request.x_min,
            request.x_max,
            y_min,
            y_max,
            max_png_bytes=request.max_png_bytes,
        )
        detail = (
            "Поверхность |f(z)| построена; полюсы и разрезы замаскированы."
            if rendering.has_points
            else "На выбранной области нет конечных значений |f(z)|."
        )
    return _plot_payload(
        plot_type=request.plot_type,
        rendering=rendering,
        variable=variable,
        x_min=request.x_min,
        x_max=request.x_max,
        y_min=request.y_min,
        y_max=request.y_max,
        detail=detail,
    )


def _resolved_y_range(request: PlotRequest) -> tuple[float, float]:
    if request.y_min is None or request.y_max is None:
        raise MathError("invalid_arguments")
    return request.y_min, request.y_max


def _plot_payload(
    *,
    plot_type: PlotType,
    rendering: PlotRendering,
    variable: str,
    x_min: float,
    x_max: float,
    y_min: float | None,
    y_max: float | None,
    detail: str,
) -> dict[str, object]:
    status = PlotStatus.OK if rendering.has_points else PlotStatus.UNDEFINED
    zero_markers = [
        MathPlotZero(re=round(real, 12), im=round(imaginary, 12))
        for real, imaginary in rendering.zero_markers
    ]
    detail_text = detail
    if zero_markers:
        marker_text = ", ".join(f"({marker.re:g}, {marker.im:g})" for marker in zero_markers)
        detail_text = f"{detail} Нули отмечены на поверхности: {marker_text}."
    result = MathPlotResult(
        status=status,
        plot_type=plot_type,
        functions=rendering.series,
        variable=variable,
        x_min=x_min,
        x_max=x_max,
        y_min=y_min,
        y_max=y_max,
        samples=rendering.samples,
        png_bytes=len(rendering.png),
        masked_points=rendering.masked_points,
        total_points=rendering.total_points,
        branch_cuts=list(rendering.branch_cuts),
        poles=list(rendering.poles),
        checkpoints=list(rendering.checkpoints),
        zero_markers=zero_markers,
        levels=list(rendering.levels),
        color_scale=rendering.color_scale,
        detail=detail_text,
    )
    return {
        "result": result.model_dump(mode="json"),
        "png_base64": _encode_png(rendering.png),
    }


def _encode_png(png: bytes) -> str:
    return base64.b64encode(png).decode("ascii")
