from collections.abc import Sequence

from sympy import Symbol

from mcp_servers.math_tools.expressions import ParsedExpression
from mcp_servers.math_tools.limits import MAX_PNG_BYTES
from mcp_servers.math_tools.plotting.common import PlotRendering
from mcp_servers.math_tools.plotting.renderers_2d import (
    render_contour,
    render_curve,
    render_domain_coloring,
    render_parametric,
)
from mcp_servers.math_tools.plotting.renderers_3d import (
    render_complex_height,
    render_surface,
)
from mcp_servers.math_tools.schemas import MathPlotSeries

__all__ = [
    "PlotRendering",
    "render_complex_height",
    "render_contour",
    "render_curve",
    "render_domain_coloring",
    "render_parametric",
    "render_plot",
    "render_surface",
]


def render_plot(
    functions: Sequence[tuple[str, ParsedExpression]],
    variable: str,
    symbol: Symbol,
    x_min: float,
    x_max: float,
    samples: int,
    max_png_bytes: int = MAX_PNG_BYTES,
) -> tuple[bytes, list[MathPlotSeries], bool]:
    rendering = render_curve(
        functions,
        variable,
        symbol,
        x_min,
        x_max,
        samples,
        max_png_bytes,
    )
    return rendering.png, rendering.series, rendering.has_points
