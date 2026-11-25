from collections.abc import Sequence

import numpy as np
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.ticker import MaxNLocator
from mpl_toolkits.mplot3d import Axes3D
from sympy import Symbol

from mcp_servers.math_tools.expressions import ParsedExpression
from mcp_servers.math_tools.limits import MAX_PNG_BYTES, SURFACE_GRID_SAMPLES
from mcp_servers.math_tools.plotting.common import PlotRendering, save_png
from mcp_servers.math_tools.plotting.features import (
    complex_checkpoints,
    complex_zero_markers,
    constraint_poles,
    surface_checkpoints,
)
from mcp_servers.math_tools.plotting.sampling import (
    complex_field,
    magnitude_mask,
    sample_real_grid,
    validate_complex_expression,
)
from mcp_servers.math_tools.schemas import MathPlotSeries


def render_surface(
    parsed: ParsedExpression,
    x_symbol: Symbol,
    y_symbol: Symbol,
    x_min: float,
    x_max: float,
    y_min: float,
    y_max: float,
    max_png_bytes: int = MAX_PNG_BYTES,
) -> PlotRendering:
    grid = sample_real_grid(
        parsed,
        x_symbol,
        y_symbol,
        x_min,
        x_max,
        y_min,
        y_max,
        SURFACE_GRID_SAMPLES,
    )
    valid = grid.domain_mask & magnitude_mask(grid.values)
    surface_values = np.ma.masked_where(~valid, grid.values)

    png, has_points = render_3d_surface(
        grid.grid_x,
        grid.grid_y,
        surface_values,
        grid.values,
        valid,
        x_label=x_symbol.name,
        y_label=y_symbol.name,
        z_label=f"f({x_symbol.name}, {y_symbol.name})",
        title=f"Surface z=f({x_symbol.name}, {y_symbol.name})",
        colorbar_label=f"f({x_symbol.name}, {y_symbol.name})",
        empty_text="no real values",
        x_min=x_min,
        x_max=x_max,
        y_min=y_min,
        y_max=y_max,
        max_png_bytes=max_png_bytes,
    )
    poles = constraint_poles(parsed.constraints)
    checkpoints = surface_checkpoints(
        parsed,
        [x_symbol, y_symbol],
        x_min,
        x_max,
        y_min,
        y_max,
    )
    series = MathPlotSeries(
        expression=parsed.text,
        segments=1 if has_points else 0,
        discontinuities=list(poles),
    )
    return PlotRendering(
        png=png,
        series=[series],
        has_points=has_points,
        samples=SURFACE_GRID_SAMPLES,
        masked_points=int((~valid).sum()),
        total_points=int(valid.size),
        poles=poles,
        checkpoints=checkpoints,
        color_scale=f"viridis: f({x_symbol.name}, {y_symbol.name})",
    )


def render_complex_height(
    parsed: ParsedExpression,
    symbol: Symbol,
    x_min: float,
    x_max: float,
    y_min: float,
    y_max: float,
    max_png_bytes: int = MAX_PNG_BYTES,
) -> PlotRendering:
    validate_complex_expression(parsed.expression, symbol)
    field = complex_field(
        parsed,
        symbol,
        x_min,
        x_max,
        y_min,
        y_max,
        SURFACE_GRID_SAMPLES,
    )
    magnitudes = np.abs(field.values)
    valid = ~field.mask & np.isfinite(magnitudes)
    heights = np.ma.masked_where(~valid, magnitudes)
    markers = complex_zero_markers(
        parsed,
        symbol,
        x_min,
        x_max,
        y_min,
        y_max,
    )

    png, has_points = render_3d_surface(
        field.grid_x,
        field.grid_y,
        heights,
        magnitudes,
        valid,
        x_label=f"Re({symbol.name})",
        y_label=f"Im({symbol.name})",
        z_label=f"|f({symbol.name})|",
        title=f"Surface |f({symbol.name})|",
        colorbar_label=f"|f({symbol.name})|",
        empty_text="no finite values",
        x_min=x_min,
        x_max=x_max,
        y_min=y_min,
        y_max=y_max,
        markers=markers,
        max_png_bytes=max_png_bytes,
    )
    checkpoints = complex_checkpoints(
        parsed,
        symbol,
        x_min,
        x_max,
        y_min,
        y_max,
        include_height=True,
    )
    series = MathPlotSeries(
        expression=parsed.text,
        segments=1 if has_points else 0,
        discontinuities=[*field.branch_cuts, *field.poles],
    )
    return PlotRendering(
        png=png,
        series=[series],
        has_points=has_points,
        samples=SURFACE_GRID_SAMPLES,
        masked_points=int(field.mask.sum()),
        total_points=int(field.mask.size),
        branch_cuts=field.branch_cuts,
        poles=field.poles,
        checkpoints=checkpoints,
        zero_markers=tuple((marker[0], marker[1]) for marker in markers),
        color_scale=f"viridis: |f({symbol.name})|",
    )


def render_3d_surface(
    grid_x: np.ndarray,
    grid_y: np.ndarray,
    surface_values: np.ma.MaskedArray,
    raw_values: np.ndarray,
    valid: np.ndarray,
    *,
    x_label: str,
    y_label: str,
    z_label: str,
    title: str,
    colorbar_label: str,
    empty_text: str,
    x_min: float,
    x_max: float,
    y_min: float,
    y_max: float,
    markers: Sequence[tuple[float, float, float]] = (),
    max_png_bytes: int = MAX_PNG_BYTES,
) -> tuple[bytes, bool]:
    figure = Figure(figsize=(6.6, 5.2), dpi=100)
    FigureCanvasAgg(figure)
    axes: Axes3D = figure.add_subplot(111, projection="3d")
    has_points = bool(valid.any())
    if has_points:
        surface = axes.plot_surface(
            grid_x,
            grid_y,
            surface_values,
            cmap="viridis",
            linewidth=0,
            antialiased=True,
        )
        colorbar = figure.colorbar(
            surface,
            ax=axes,
            fraction=0.035,
            pad=0.10,
            shrink=0.8,
        )
        colorbar.set_label(colorbar_label)
        colorbar.ax.tick_params(labelsize=8)
        set_surface_limits(axes, raw_values[valid])
        if markers:
            marker_x = [marker[0] for marker in markers]
            marker_y = [marker[1] for marker in markers]
            marker_z = [marker[2] for marker in markers]
            axes.scatter(
                marker_x,
                marker_y,
                marker_z,
                color="#d62728",
                s=28,
                depthshade=False,
                label="zeros",
            )
            axes.legend(loc="upper right", fontsize=8)
    else:
        axes.text2D(0.5, 0.5, empty_text, transform=axes.transAxes, ha="center")

    axes.set_xlim(x_min, x_max)
    axes.set_ylim(y_min, y_max)
    axes.set_xlabel(x_label, labelpad=8)
    axes.set_ylabel(y_label, labelpad=8)
    axes.set_zlabel(z_label, labelpad=8)
    axes.set_title(title)
    for axis in (axes.xaxis, axes.yaxis, axes.zaxis):
        axis.set_major_locator(MaxNLocator(nbins=5))
    axes.tick_params(labelsize=8)
    return save_png(figure, max_png_bytes), has_points


def set_surface_limits(axes: Axes3D, values: np.ndarray) -> None:
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return
    low = float(np.percentile(finite, 1))
    high = float(np.percentile(finite, 99))
    if high - low <= 1e-12:
        low -= 1.0
        high += 1.0
    axes.set_zlim(low, high)
