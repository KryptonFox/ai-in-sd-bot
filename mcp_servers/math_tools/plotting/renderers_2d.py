import math
from collections.abc import Sequence

import numpy as np
from matplotlib.axes import Axes
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.cm import ScalarMappable
from matplotlib.colors import Normalize, hsv_to_rgb
from matplotlib.figure import Figure
from sympy import Symbol

from mcp_servers.math_tools.expressions import ParsedExpression
from mcp_servers.math_tools.limits import GRID_SAMPLES, MAX_PNG_BYTES
from mcp_servers.math_tools.plotting.common import (
    COLORS,
    PlotRendering,
    format_position,
    save_png,
    short_label,
)
from mcp_servers.math_tools.plotting.features import (
    complex_checkpoints,
    constraint_poles,
    curve_checkpoints,
    parametric_checkpoint,
    surface_checkpoints,
)
from mcp_servers.math_tools.plotting.sampling import (
    complex_field,
    constraint_mask,
    count_boolean_segments,
    evaluate_real,
    sample_real_grid,
    split_discontinuities,
    transition_positions,
    validate_complex_expression,
)
from mcp_servers.math_tools.schemas import MathPlotSeries


def render_curve(
    functions: Sequence[tuple[str, ParsedExpression]],
    variable: str,
    symbol: Symbol,
    x_min: float,
    x_max: float,
    samples: int,
    max_png_bytes: int = MAX_PNG_BYTES,
) -> PlotRendering:
    x_values = np.linspace(x_min, x_max, samples)
    midpoints = (x_values[:-1] + x_values[1:]) / 2

    figure = Figure(figsize=(6.4, 4.2), dpi=100)
    FigureCanvasAgg(figure)
    axes = figure.add_subplot(111)

    rendered_series: list[MathPlotSeries] = []
    plotted_values: list[np.ndarray] = []

    for index, (text, parsed) in enumerate(functions):
        values = evaluate_real(parsed.expression, [symbol], [x_values])
        middle_values = evaluate_real(parsed.expression, [symbol], [midpoints])
        domain_mask = constraint_mask(parsed.constraints, [symbol], [x_values])
        middle_domain_mask = constraint_mask(parsed.constraints, [symbol], [midpoints])
        values = np.where(domain_mask, values, np.nan)
        middle_values = np.where(middle_domain_mask, middle_values, np.nan)
        split_values, discontinuities = split_discontinuities(
            x_values,
            values,
            middle_values,
        )
        segments = count_boolean_segments(np.isfinite(split_values))
        rendered_series.append(
            MathPlotSeries(
                expression=text,
                segments=segments,
                discontinuities=[format_position(value) for value in discontinuities],
            )
        )
        plotted_values.append(split_values)
        axes.plot(
            x_values,
            split_values,
            color=COLORS[index % len(COLORS)],
            linewidth=1.6,
            label=short_label(text),
        )

    configure_curve_axes(axes, variable, x_min, x_max, plotted_values)
    has_points = any(series.segments > 0 for series in rendered_series)
    checkpoints = curve_checkpoints(functions, symbol, x_min, x_max)
    return PlotRendering(
        png=save_png(figure, max_png_bytes),
        series=rendered_series,
        has_points=has_points,
        samples=samples,
        total_points=len(x_values),
        checkpoints=checkpoints,
    )


def render_parametric(
    x_parsed: ParsedExpression,
    y_parsed: ParsedExpression,
    parameter: str,
    symbol: Symbol,
    t_min: float,
    t_max: float,
    samples: int,
    max_png_bytes: int = MAX_PNG_BYTES,
) -> PlotRendering:
    t_values = np.linspace(t_min, t_max, samples)
    x_values = evaluate_real(x_parsed.expression, [symbol], [t_values])
    y_values = evaluate_real(y_parsed.expression, [symbol], [t_values])
    x_mask = constraint_mask(x_parsed.constraints, [symbol], [t_values])
    y_mask = constraint_mask(y_parsed.constraints, [symbol], [t_values])
    valid = np.isfinite(x_values) & np.isfinite(y_values) & x_mask & y_mask

    figure = Figure(figsize=(6.4, 5.4), dpi=100)
    FigureCanvasAgg(figure)
    axes = figure.add_subplot(111)

    broken_x = np.where(valid, x_values, np.nan)
    broken_y = np.where(valid, y_values, np.nan)
    axes.plot(broken_x, broken_y, color=COLORS[0], linewidth=1.8, label="(x(t), y(t))")
    segments = count_boolean_segments(valid)
    discontinuities = transition_positions(t_values, valid)
    configure_parametric_axes(axes, x_values, y_values, parameter, t_min, t_max)

    expression = f"{short_label(x_parsed.text)}, {short_label(y_parsed.text)}"
    series = MathPlotSeries(
        expression=expression,
        segments=segments,
        discontinuities=[format_position(value) for value in discontinuities],
    )
    checkpoint = parametric_checkpoint(x_parsed, y_parsed, symbol, t_min, t_max)
    return PlotRendering(
        png=save_png(figure, max_png_bytes),
        series=[series],
        has_points=segments > 0,
        samples=samples,
        total_points=len(t_values),
        checkpoints=(checkpoint,),
    )


def render_contour(
    parsed: ParsedExpression,
    x_symbol: Symbol,
    y_symbol: Symbol,
    x_min: float,
    x_max: float,
    y_min: float,
    y_max: float,
    requested_levels: Sequence[float] | None,
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
        GRID_SAMPLES,
    )
    finite = np.isfinite(grid.values) & grid.domain_mask
    masked_values = np.ma.masked_where(~finite, grid.values)

    figure = Figure(figsize=(6.4, 5.0), dpi=100)
    FigureCanvasAgg(figure)
    axes = figure.add_subplot(111)

    line_levels = resolve_levels(grid.values[finite], requested_levels)
    fill_levels = (
        line_levels if len(line_levels) >= 2 else resolve_levels(grid.values[finite], None)
    )
    has_points = bool(finite.any()) and bool(line_levels)

    if has_points:
        contour = axes.contourf(
            grid.grid_x,
            grid.grid_y,
            masked_values,
            levels=fill_levels,
            cmap="viridis",
            extend="both",
        )
        lines = axes.contour(
            grid.grid_x,
            grid.grid_y,
            masked_values,
            levels=line_levels,
            colors="black",
            linewidths=0.8,
        )
        try:
            axes.clabel(lines, inline=True, fontsize=7, fmt="%1.3g")
        except (TypeError, ValueError):
            pass
        colorbar = figure.colorbar(contour, ax=axes)
        colorbar.set_label(f"f({x_symbol.name}, {y_symbol.name})")
    else:
        axes.text(0.5, 0.5, "no real values", transform=axes.transAxes, ha="center")

    axes.set_xlim(x_min, x_max)
    axes.set_ylim(y_min, y_max)
    axes.set_xlabel(x_symbol.name)
    axes.set_ylabel(y_symbol.name)
    axes.set_title("Contour plot")
    axes.grid(True, alpha=0.2)

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
        png=save_png(figure, max_png_bytes),
        series=[series],
        has_points=has_points,
        samples=GRID_SAMPLES,
        masked_points=int((~finite).sum()),
        total_points=int(finite.size),
        poles=poles,
        checkpoints=checkpoints,
        levels=line_levels,
        color_scale=f"viridis: f({x_symbol.name}, {y_symbol.name})",
    )


def render_domain_coloring(
    parsed: ParsedExpression,
    symbol: Symbol,
    x_min: float,
    x_max: float,
    y_min: float,
    y_max: float,
    max_png_bytes: int = MAX_PNG_BYTES,
) -> PlotRendering:
    validate_complex_expression(parsed.expression, symbol)
    field = complex_field(parsed, symbol, x_min, x_max, y_min, y_max, GRID_SAMPLES)
    finite = ~field.mask
    visible = np.isfinite(field.values.real) & np.isfinite(field.values.imag) & finite

    figure = Figure(figsize=(6.4, 5.2), dpi=100)
    FigureCanvasAgg(figure)
    axes = figure.add_subplot(111)

    if visible.any():
        image = domain_coloring_rgb(field.values, visible)
        axes.imshow(
            image,
            extent=(x_min, x_max, y_min, y_max),
            origin="lower",
            interpolation="nearest",
            aspect="equal",
        )
        phase_scale = ScalarMappable(norm=Normalize(-math.pi, math.pi), cmap="hsv")
        colorbar = figure.colorbar(phase_scale, ax=axes, fraction=0.046, pad=0.04)
        colorbar.set_label(f"arg f({symbol.name})")
    else:
        axes.text(0.5, 0.5, "no finite values", transform=axes.transAxes, ha="center")
        axes.set_xlim(x_min, x_max)
        axes.set_ylim(y_min, y_max)

    axes.set_xlabel(f"Re({symbol.name})")
    axes.set_ylabel(f"Im({symbol.name})")
    axes.set_title("Domain coloring: hue=arg, brightness=normalized |f|")
    checkpoints = complex_checkpoints(
        parsed,
        symbol,
        x_min,
        x_max,
        y_min,
        y_max,
        include_height=False,
    )
    series = MathPlotSeries(
        expression=parsed.text,
        segments=1 if visible.any() else 0,
        discontinuities=[*field.branch_cuts, *field.poles],
    )
    return PlotRendering(
        png=save_png(figure, max_png_bytes),
        series=[series],
        has_points=bool(visible.any()),
        samples=GRID_SAMPLES,
        masked_points=int(field.mask.sum()),
        total_points=int(field.mask.size),
        branch_cuts=field.branch_cuts,
        poles=field.poles,
        checkpoints=checkpoints,
        color_scale=(f"hue=arg f({symbol.name}), brightness=normalized |f({symbol.name})|"),
    )


def domain_coloring_rgb(values: np.ndarray, visible: np.ndarray) -> np.ndarray:
    safe_values = np.where(visible, values, 1.0 + 0.0j)
    phase = np.angle(safe_values)
    magnitude = np.abs(safe_values)
    hue = ((phase + math.pi) / (2 * math.pi)) % 1.0
    shaped = (2 / math.pi) * np.arctan(np.log1p(magnitude))
    brightness = 0.35 + 0.55 * shaped
    saturation = np.full_like(brightness, 0.9)
    colors = hsv_to_rgb(np.stack([hue, saturation, brightness], axis=-1))

    image = np.ones((*values.shape, 4), dtype=float)
    image[..., :3] = colors
    image[~visible, :3] = 0.86
    image[~visible, 3] = 1.0
    return image


def resolve_levels(
    values: np.ndarray,
    requested: Sequence[float] | None,
) -> tuple[float, ...]:
    if requested is not None:
        return tuple(sorted({float(level) for level in requested}))
    if values.size == 0:
        return ()
    low = float(np.min(values))
    high = float(np.max(values))
    if not math.isfinite(low) or not math.isfinite(high):
        return ()
    if high - low <= 1e-12:
        return (low - 0.5, low + 0.5)
    return tuple(float(level) for level in np.linspace(low, high, 13))


def configure_curve_axes(
    axes: Axes,
    variable: str,
    x_min: float,
    x_max: float,
    plotted_values: Sequence[np.ndarray],
) -> None:
    axes.set_xlim(x_min, x_max)
    axes.grid(True, alpha=0.3)
    axes.axhline(0, color="#444444", linewidth=0.8)
    axes.axvline(0, color="#444444", linewidth=0.8)
    axes.set_xlabel(variable)
    axes.set_ylabel("y")
    axes.set_title("Math plot")
    axes.legend(loc="best", fontsize=8)

    finite_arrays = [
        values[np.isfinite(values)] for values in plotted_values if np.isfinite(values).any()
    ]
    if not finite_arrays:
        axes.set_ylim(-1.0, 1.0)
        return

    finite_values = np.concatenate(finite_arrays)
    low = float(np.min(finite_values))
    high = float(np.max(finite_values))
    if high - low > 1e4:
        low = float(np.percentile(finite_values, 2))
        high = float(np.percentile(finite_values, 98))
    if not math.isfinite(low) or not math.isfinite(high):
        low, high = -1.0, 1.0
    if high - low <= 1e-12:
        low -= 1.0
        high += 1.0
    margin = 0.05 * (high - low)
    axes.set_ylim(low - margin, high + margin)


def configure_parametric_axes(
    axes: Axes,
    x_values: np.ndarray,
    y_values: np.ndarray,
    parameter: str,
    t_min: float,
    t_max: float,
) -> None:
    finite = np.isfinite(x_values) & np.isfinite(y_values)
    axes.grid(True, alpha=0.3)
    axes.axhline(0, color="#444444", linewidth=0.8)
    axes.axvline(0, color="#444444", linewidth=0.8)
    axes.set_xlabel("x")
    axes.set_ylabel("y")
    axes.set_title(
        f"Parametric curve, {parameter} in [{format_position(t_min)}, {format_position(t_max)}]"
    )
    axes.legend(loc="best", fontsize=8)
    axes.set_aspect("equal")

    if not finite.any():
        axes.set_xlim(-1.0, 1.0)
        axes.set_ylim(-1.0, 1.0)
        return

    x_finite = x_values[finite]
    y_finite = y_values[finite]
    x_low, x_high = float(np.min(x_finite)), float(np.max(x_finite))
    y_low, y_high = float(np.min(y_finite)), float(np.max(y_finite))
    if x_high - x_low <= 1e-12:
        x_low, x_high = x_low - 1.0, x_high + 1.0
    if y_high - y_low <= 1e-12:
        y_low, y_high = y_low - 1.0, y_high + 1.0
    axes.set_xlim(x_low - 0.05 * (x_high - x_low), x_high + 0.05 * (x_high - x_low))
    axes.set_ylim(y_low - 0.05 * (y_high - y_low), y_high + 0.05 * (y_high - y_low))
