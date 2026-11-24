import io
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np
import sympy
from matplotlib.axes import Axes
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.cm import ScalarMappable
from matplotlib.colors import Normalize, hsv_to_rgb
from matplotlib.figure import Figure
from matplotlib.ticker import MaxNLocator
from mpl_toolkits.mplot3d import Axes3D
from sympy import Expr, Symbol, lambdify

from mcp_servers.math_tools.errors import MathError
from mcp_servers.math_tools.expressions import (
    DomainConstraint,
    DomainRelation,
    ParsedExpression,
)
from mcp_servers.math_tools.limits import (
    GRID_SAMPLES,
    MAX_PNG_BYTES,
    POLE_MAGNITUDE,
    SURFACE_GRID_SAMPLES,
)
from mcp_servers.math_tools.schemas import MathPlotCheckpoint, MathPlotSeries

COLORS = ("#1f77b4", "#d62728", "#2ca02c", "#9467bd")
ELLIPSIS_LENGTH = 48
BRANCH_CUT_WIDTH_CELLS = 0.75
MAX_LISTED_POLES = 8


@dataclass(frozen=True)
class PlotRendering:
    png: bytes
    series: list[MathPlotSeries]
    has_points: bool
    samples: int
    masked_points: int = 0
    total_points: int = 0
    branch_cuts: tuple[str, ...] = ()
    poles: tuple[str, ...] = ()
    checkpoints: tuple[MathPlotCheckpoint, ...] = ()
    zero_markers: tuple[tuple[float, float], ...] = ()
    levels: tuple[float, ...] = ()
    color_scale: str = ""


@dataclass(frozen=True)
class _ComplexField:
    values: np.ndarray
    mask: np.ndarray
    branch_cuts: tuple[str, ...]
    poles: tuple[str, ...]


def render_plot(
    functions: Sequence[tuple[str, ParsedExpression]],
    variable: str,
    symbol: Symbol,
    x_min: float,
    x_max: float,
    samples: int,
) -> tuple[bytes, list[MathPlotSeries], bool]:
    rendering = render_curve(functions, variable, symbol, x_min, x_max, samples)
    return rendering.png, rendering.series, rendering.has_points


def render_curve(
    functions: Sequence[tuple[str, ParsedExpression]],
    variable: str,
    symbol: Symbol,
    x_min: float,
    x_max: float,
    samples: int,
) -> PlotRendering:
    x_values = np.linspace(x_min, x_max, samples)
    midpoints = (x_values[:-1] + x_values[1:]) / 2

    figure = Figure(figsize=(6.4, 4.2), dpi=100)
    FigureCanvasAgg(figure)
    axes = figure.add_subplot(111)

    rendered_series: list[MathPlotSeries] = []
    plotted_values: list[np.ndarray] = []

    for index, (text, parsed) in enumerate(functions):
        values = _evaluate_real(parsed.expression, [symbol], [x_values])
        middle_values = _evaluate_real(parsed.expression, [symbol], [midpoints])
        domain_mask = _constraint_mask(parsed.constraints, [symbol], [x_values])
        middle_domain_mask = _constraint_mask(parsed.constraints, [symbol], [midpoints])
        values = np.where(domain_mask, values, np.nan)
        middle_values = np.where(middle_domain_mask, middle_values, np.nan)
        split_values, discontinuities = _split_discontinuities(
            x_values,
            values,
            middle_values,
        )
        segments = _count_segments(split_values)
        rendered_series.append(
            MathPlotSeries(
                expression=text,
                segments=segments,
                discontinuities=[_format_position(value) for value in discontinuities],
            )
        )
        plotted_values.append(split_values)
        label = _short_label(text)
        axes.plot(
            x_values,
            split_values,
            color=COLORS[index % len(COLORS)],
            linewidth=1.6,
            label=label,
        )

    _configure_axes(axes, variable, x_min, x_max, plotted_values)
    has_points = any(series.segments > 0 for series in rendered_series)
    checkpoints = _curve_checkpoints(functions, symbol, x_min, x_max)
    return PlotRendering(
        png=_save_png(figure),
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
) -> PlotRendering:
    t_values = np.linspace(t_min, t_max, samples)
    x_values = _evaluate_real(x_parsed.expression, [symbol], [t_values])
    y_values = _evaluate_real(y_parsed.expression, [symbol], [t_values])
    x_mask = _constraint_mask(x_parsed.constraints, [symbol], [t_values])
    y_mask = _constraint_mask(y_parsed.constraints, [symbol], [t_values])
    valid = np.isfinite(x_values) & np.isfinite(y_values) & x_mask & y_mask

    figure = Figure(figsize=(6.4, 5.4), dpi=100)
    FigureCanvasAgg(figure)
    axes = figure.add_subplot(111)

    broken_x = np.where(valid, x_values, np.nan)
    broken_y = np.where(valid, y_values, np.nan)
    axes.plot(broken_x, broken_y, color=COLORS[0], linewidth=1.8, label="(x(t), y(t))")
    segments = _count_boolean_segments(valid)
    discontinuities = _transition_positions(t_values, valid)
    _configure_parametric_axes(axes, x_values, y_values, parameter, t_min, t_max)

    expression = f"{_short_label(x_parsed.text)}, {_short_label(y_parsed.text)}"
    series = MathPlotSeries(
        expression=expression,
        segments=segments,
        discontinuities=[_format_position(value) for value in discontinuities],
    )
    checkpoint = _parametric_checkpoint(x_parsed, y_parsed, symbol, t_min, t_max)
    return PlotRendering(
        png=_save_png(figure),
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
) -> PlotRendering:
    x_values = np.linspace(x_min, x_max, GRID_SAMPLES)
    y_values = np.linspace(y_min, y_max, GRID_SAMPLES)
    grid_x, grid_y = np.meshgrid(x_values, y_values)
    values = _evaluate_real(parsed.expression, [x_symbol, y_symbol], [grid_x, grid_y])
    domain_mask = _constraint_mask(parsed.constraints, [x_symbol, y_symbol], [grid_x, grid_y])
    finite = np.isfinite(values) & domain_mask
    masked_values = np.ma.masked_where(~finite, values)

    figure = Figure(figsize=(6.4, 5.0), dpi=100)
    FigureCanvasAgg(figure)
    axes = figure.add_subplot(111)
    resolved_levels = _resolve_levels(values[finite], requested_levels)
    line_levels = (
        tuple(float(level) for level in requested_levels)
        if requested_levels is not None
        else resolved_levels
    )
    fill_levels = (
        resolved_levels
        if len(resolved_levels) >= 2
        else _resolve_levels(
            values[finite],
            None,
        )
    )
    has_points = bool(finite.any()) and bool(line_levels)

    if has_points:
        contour = axes.contourf(
            grid_x,
            grid_y,
            masked_values,
            levels=fill_levels,
            cmap="viridis",
            extend="both",
        )
        lines = axes.contour(
            grid_x,
            grid_y,
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
        colorbar.set_label("f(x, y)")
    else:
        axes.text(0.5, 0.5, "no real values", transform=axes.transAxes, ha="center")

    axes.set_xlim(x_min, x_max)
    axes.set_ylim(y_min, y_max)
    axes.set_xlabel(x_symbol.name)
    axes.set_ylabel(y_symbol.name)
    axes.set_title("Contour plot")
    axes.grid(True, alpha=0.2)

    poles = _constraint_poles(parsed.constraints)
    checkpoints = _surface_checkpoints(
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
        png=_save_png(figure),
        series=[series],
        has_points=has_points,
        samples=GRID_SAMPLES,
        masked_points=int((~finite).sum()),
        total_points=int(finite.size),
        poles=poles,
        checkpoints=checkpoints,
        levels=line_levels,
        color_scale="viridis: f(x, y)",
    )


def render_domain_coloring(
    parsed: ParsedExpression,
    symbol: Symbol,
    x_min: float,
    x_max: float,
    y_min: float,
    y_max: float,
) -> PlotRendering:
    field = _complex_field(parsed, symbol, x_min, x_max, y_min, y_max, GRID_SAMPLES)
    finite = ~field.mask
    visible = np.isfinite(field.values.real) & np.isfinite(field.values.imag) & finite

    figure = Figure(figsize=(6.4, 5.2), dpi=100)
    FigureCanvasAgg(figure)
    axes = figure.add_subplot(111)

    if visible.any():
        image = _domain_coloring_rgb(field.values, visible)
        axes.imshow(
            image,
            extent=(x_min, x_max, y_min, y_max),
            origin="lower",
            interpolation="nearest",
            aspect="equal",
        )
        phase_scale = ScalarMappable(norm=Normalize(-math.pi, math.pi), cmap="hsv")
        colorbar = figure.colorbar(phase_scale, ax=axes, fraction=0.046, pad=0.04)
        colorbar.set_label("arg f(z)")
    else:
        axes.text(0.5, 0.5, "no finite values", transform=axes.transAxes, ha="center")
        axes.set_xlim(x_min, x_max)
        axes.set_ylim(y_min, y_max)

    axes.set_xlabel("Re(z)")
    axes.set_ylabel("Im(z)")
    axes.set_title("Domain coloring: hue=arg, brightness=normalized |f|")
    checkpoints = _complex_checkpoints(
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
        png=_save_png(figure),
        series=[series],
        has_points=bool(visible.any()),
        samples=GRID_SAMPLES,
        masked_points=int(field.mask.sum()),
        total_points=int(field.mask.size),
        branch_cuts=field.branch_cuts,
        poles=field.poles,
        checkpoints=checkpoints,
        color_scale="hue=arg f(z), brightness=normalized |f(z)|",
    )


def render_surface(
    parsed: ParsedExpression,
    x_symbol: Symbol,
    y_symbol: Symbol,
    x_min: float,
    x_max: float,
    y_min: float,
    y_max: float,
) -> PlotRendering:
    x_values = np.linspace(x_min, x_max, SURFACE_GRID_SAMPLES)
    y_values = np.linspace(y_min, y_max, SURFACE_GRID_SAMPLES)
    grid_x, grid_y = np.meshgrid(x_values, y_values)
    values = _evaluate_real(parsed.expression, [x_symbol, y_symbol], [grid_x, grid_y])
    domain_mask = _constraint_mask(parsed.constraints, [x_symbol, y_symbol], [grid_x, grid_y])
    valid = np.isfinite(values) & domain_mask & _magnitude_mask(values)
    surface_values = np.ma.masked_where(~valid, values)

    png, has_points = _render_3d_surface(
        grid_x,
        grid_y,
        surface_values,
        values,
        valid,
        x_label=x_symbol.name,
        y_label=y_symbol.name,
        z_label="f(x, y)",
        title="Surface z=f(x, y)",
        colorbar_label="f(x, y)",
        empty_text="no real values",
        x_min=x_min,
        x_max=x_max,
        y_min=y_min,
        y_max=y_max,
    )
    poles = _constraint_poles(parsed.constraints)
    checkpoints = _surface_checkpoints(
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
        color_scale="viridis: f(x, y)",
    )


def render_complex_height(
    parsed: ParsedExpression,
    symbol: Symbol,
    x_min: float,
    x_max: float,
    y_min: float,
    y_max: float,
) -> PlotRendering:
    field = _complex_field(
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

    x_values = np.linspace(x_min, x_max, SURFACE_GRID_SAMPLES)
    y_values = np.linspace(y_min, y_max, SURFACE_GRID_SAMPLES)
    grid_x, grid_y = np.meshgrid(x_values, y_values)
    markers = _complex_zero_markers(
        parsed,
        symbol,
        x_min,
        x_max,
        y_min,
        y_max,
    )

    png, has_points = _render_3d_surface(
        grid_x,
        grid_y,
        heights,
        magnitudes,
        valid,
        x_label="Re(z)",
        y_label="Im(z)",
        z_label="|f(z)|",
        title="Surface z=|f(z)|",
        colorbar_label="|f(z)|",
        empty_text="no finite values",
        x_min=x_min,
        x_max=x_max,
        y_min=y_min,
        y_max=y_max,
        markers=markers,
    )
    checkpoints = _complex_checkpoints(
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
        color_scale="viridis: |f(z)|",
    )


def _render_3d_surface(
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
) -> tuple[bytes, bool]:
    figure = Figure(figsize=(6.6, 5.2), dpi=100)
    FigureCanvasAgg(figure)
    axes = figure.add_subplot(111, projection="3d")
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
        _set_surface_limits(axes, raw_values[valid])
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
    return _save_png(figure), has_points


def _complex_zero_markers(
    parsed: ParsedExpression,
    symbol: Symbol,
    x_min: float,
    x_max: float,
    y_min: float,
    y_max: float,
) -> tuple[tuple[float, float, float], ...]:
    try:
        roots = sympy.solve(parsed.expression, symbol)
    except (NotImplementedError, TypeError, ValueError):
        return ()
    markers: list[tuple[float, float, float]] = []
    for root in roots:
        numeric = _finite_number(root)
        if numeric is None:
            continue
        if not (x_min <= numeric.real <= x_max and y_min <= numeric.imag <= y_max):
            continue
        substitution = {symbol: root}
        try:
            original_value = parsed.original.subs(list(substitution.items()))
        except (TypeError, ValueError):
            continue
        if original_value is sympy.zoo or original_value.has(sympy.zoo, sympy.nan):
            continue
        if _denominator_violated(parsed.constraints, substitution):
            continue
        if _on_branch_cut(parsed.expression, symbol, root):
            continue
        markers.append((numeric.real, numeric.imag, 0.0))
        if len(markers) >= 8:
            break
    return tuple(markers)


def _complex_field(
    parsed: ParsedExpression,
    symbol: Symbol,
    x_min: float,
    x_max: float,
    y_min: float,
    y_max: float,
    samples: int,
) -> _ComplexField:
    x_values = np.linspace(x_min, x_max, samples)
    y_values = np.linspace(y_min, y_max, samples)
    grid_x, grid_y = np.meshgrid(x_values, y_values)
    values = _evaluate_complex(parsed.expression, [symbol], [grid_x + 1j * grid_y])
    finite = np.isfinite(values.real) & np.isfinite(values.imag)
    pole_mask = ~finite | (np.abs(values) > POLE_MAGNITUDE)

    branch_mask, branch_cuts = _branch_cut_mask(
        parsed.expression,
        symbol,
        [grid_x + 1j * grid_y],
        grid_y,
    )
    poles = _symbolic_poles(parsed.expression, symbol, x_min, x_max, y_min, y_max)
    return _ComplexField(
        values=values,
        mask=pole_mask | branch_mask,
        branch_cuts=branch_cuts,
        poles=poles,
    )


def _branch_cut_mask(
    expression: Expr,
    symbol: Symbol,
    arrays: Sequence[np.ndarray],
    grid_y: np.ndarray,
) -> tuple[np.ndarray, tuple[str, ...]]:
    mask = np.zeros(grid_y.shape, dtype=bool)
    cuts: list[str] = []
    step = float(np.mean(np.diff(grid_y[:, 0]))) if grid_y.shape[0] > 1 else 0.01
    tolerance = max(abs(step) * BRANCH_CUT_WIDTH_CELLS, 1e-12)

    for logarithm in expression.atoms(sympy.log):
        if not isinstance(logarithm, sympy.log):
            continue
        argument = logarithm.args[0]
        if not argument.has(symbol):
            continue
        argument_values = _evaluate_complex(argument, [symbol], arrays)
        real = argument_values.real
        imaginary = argument_values.imag
        on_cut = (real < 0) & (np.abs(imaginary) <= tolerance)
        mask |= on_cut
        cuts.append(f"log({sympy.sstr(argument)}): arg={sympy.sstr(sympy.pi)}")

    return mask, tuple(dict.fromkeys(cuts))


def _domain_coloring_rgb(values: np.ndarray, visible: np.ndarray) -> np.ndarray:
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


def _symbolic_poles(
    expression: Expr,
    symbol: Symbol,
    x_min: float,
    x_max: float,
    y_min: float,
    y_max: float,
) -> tuple[str, ...]:
    try:
        _, denominator = sympy.fraction(sympy.together(expression))
    except (TypeError, ValueError):
        return ()
    if denominator == 1 or not denominator.has(symbol):
        return ()

    try:
        roots = sympy.solve(denominator, symbol)
    except (NotImplementedError, TypeError, ValueError):
        return ()

    poles: list[str] = []
    for root in roots:
        if not isinstance(root, Expr) or root.free_symbols:
            continue
        if root.has(sympy.I) and root.is_real is not True:
            real = float(sympy.re(root).evalf())
            imaginary = float(sympy.im(root).evalf())
            if not (x_min <= real <= x_max and y_min <= imaginary <= y_max):
                continue
        else:
            numeric = _finite_number(root)
            if numeric is None or not x_min <= numeric.real <= x_max:
                continue
            if not y_min <= numeric.imag <= y_max:
                continue
        formatted = _format_complex(root)
        if formatted not in poles:
            poles.append(formatted)
        if len(poles) >= MAX_LISTED_POLES:
            break
    return tuple(poles)


def _finite_number(value: Expr) -> complex | None:
    try:
        numeric = complex(sympy.N(value, 15))
    except (TypeError, ValueError, OverflowError):
        return None
    if not (math.isfinite(numeric.real) and math.isfinite(numeric.imag)):
        return None
    return numeric


def _format_complex(value: Expr) -> str:
    return "z=" + sympy.sstr(sympy.simplify(value))


def _constraint_poles(constraints: Sequence[DomainConstraint]) -> tuple[str, ...]:
    poles = [
        f"{sympy.sstr(constraint.expression)} != 0"
        for constraint in constraints
        if constraint.relation is DomainRelation.NONZERO
    ]
    return tuple(dict.fromkeys(poles))


def _resolve_levels(values: np.ndarray, requested: Sequence[float] | None) -> tuple[float, ...]:
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


def _magnitude_mask(values: np.ndarray) -> np.ndarray:
    finite = np.isfinite(values)
    if not finite.any():
        return finite
    magnitudes = np.abs(values[finite])
    scale = float(np.median(magnitudes))
    threshold = max(POLE_MAGNITUDE, 1e4 * max(scale, 1.0))
    return ~np.isfinite(values) | (np.abs(values) <= threshold)


def _evaluate_real(
    expression: Expr,
    symbols: Sequence[Symbol],
    arrays: Sequence[np.ndarray],
) -> np.ndarray:
    try:
        function = lambdify(list(symbols), expression, modules=["numpy"])
    except (KeyError, TypeError, ValueError):
        return np.full(arrays[0].shape, np.nan)

    try:
        with np.errstate(all="ignore"):
            raw_values = function(*arrays)
    except (ArithmeticError, TypeError, ValueError):
        return np.full(arrays[0].shape, np.nan)

    array = np.asarray(raw_values)
    if array.shape == ():
        array = np.full(arrays[0].shape, array.item())

    try:
        if np.iscomplexobj(array):
            complex_values = np.asarray(array, dtype=complex)
            real = np.real(complex_values)
            imaginary = np.imag(complex_values)
            tolerance = 1e-9 * np.maximum(1.0, np.abs(real))
            values = np.where(
                np.isfinite(real) & np.isfinite(imaginary) & (np.abs(imaginary) <= tolerance),
                real,
                np.nan,
            )
        else:
            values = np.asarray(array, dtype=float)
    except (TypeError, ValueError):
        return np.full(arrays[0].shape, np.nan)

    values = np.asarray(values, dtype=float)
    values[~np.isfinite(values)] = np.nan
    return values


def _evaluate_complex(
    expression: Expr,
    symbols: Sequence[Symbol],
    arrays: Sequence[np.ndarray],
) -> np.ndarray:
    try:
        function = lambdify(list(symbols), expression, modules=["numpy"])
    except (KeyError, TypeError, ValueError):
        return np.full(arrays[0].shape, np.nan + 1j * np.nan)

    try:
        with np.errstate(all="ignore"):
            raw_values = function(*arrays)
    except (ArithmeticError, TypeError, ValueError):
        return np.full(arrays[0].shape, np.nan + 1j * np.nan)

    try:
        values = np.asarray(raw_values, dtype=complex)
    except (TypeError, ValueError):
        return np.full(arrays[0].shape, np.nan + 1j * np.nan)
    if values.shape == ():
        values = np.full(arrays[0].shape, values.item())
    values[~np.isfinite(values.real) | ~np.isfinite(values.imag)] = np.nan + 1j * np.nan
    return values


def _constraint_mask(
    constraints: Sequence[DomainConstraint],
    symbols: Sequence[Symbol],
    arrays: Sequence[np.ndarray],
) -> np.ndarray:
    mask = np.ones(arrays[0].shape, dtype=bool)
    for constraint in constraints:
        values = _evaluate_real(constraint.expression, symbols, arrays)
        finite = np.isfinite(values)
        relation = constraint.relation
        if relation is DomainRelation.NONZERO:
            valid = finite & (values != 0)
        elif relation is DomainRelation.POSITIVE:
            valid = finite & (values > 0)
        elif relation is DomainRelation.NONNEGATIVE:
            valid = finite & (values >= 0)
        elif relation is DomainRelation.NEGATIVE:
            valid = finite & (values < 0)
        elif relation is DomainRelation.NONPOSITIVE:
            valid = finite & (values <= 0)
        else:
            valid = np.zeros(arrays[0].shape, dtype=bool)
        mask &= valid
    return mask


def _split_discontinuities(
    x_values: np.ndarray,
    values: np.ndarray,
    middle_values: np.ndarray,
) -> tuple[np.ndarray, list[float]]:
    finite = np.isfinite(values)
    finite_values = np.abs(values[finite])
    scale = float(np.median(finite_values)) if finite_values.size else 1.0
    if not math.isfinite(scale) or scale <= 0:
        scale = 1.0

    break_intervals: set[int] = set()
    for index in range(len(values) - 1):
        if not finite[index] or not finite[index + 1]:
            continue
        left = float(values[index])
        right = float(values[index + 1])
        middle = float(middle_values[index])
        span = max(abs(left), abs(right))

        if not math.isfinite(middle):
            break_intervals.add(index)
            continue
        if abs(middle) > 8 * span + 1e-12:
            break_intervals.add(index)
            continue
        if (
            left * right < 0
            and min(abs(left), abs(right)) > 4 * scale
            and abs(left - right) > 20 * scale
        ):
            break_intervals.add(index)
            continue
        if span > 1e6 * scale and abs(left - right) > 1e5 * scale:
            break_intervals.add(index)

    split_values = values.copy()
    for index in break_intervals:
        split_values[index] = np.nan
        split_values[index + 1] = np.nan

    discontinuities = _discontinuity_positions(x_values, finite, break_intervals)
    return split_values, discontinuities


def _discontinuity_positions(
    x_values: np.ndarray,
    finite: np.ndarray,
    break_intervals: set[int],
) -> list[float]:
    positions: list[float] = []
    index = 0
    length = len(finite)

    while index < length:
        if finite[index]:
            index += 1
            continue
        start = index
        while index + 1 < length and not finite[index + 1]:
            index += 1
        end = index

        if start == end and 0 < start < length - 1:
            positions.append(float(x_values[start]))
        elif start == 0 and end + 1 < length:
            positions.append(float(x_values[end + 1]))
        elif end == length - 1 and start > 0:
            positions.append(float(x_values[start - 1]))
        index += 1

    for interval in break_intervals:
        positions.append(float((x_values[interval] + x_values[interval + 1]) / 2))

    return _deduplicate_positions(positions)


def _transition_positions(values: np.ndarray, valid: np.ndarray) -> list[float]:
    positions: list[float] = []
    for index in range(len(valid) - 1):
        if bool(valid[index]) != bool(valid[index + 1]):
            positions.append(float((values[index] + values[index + 1]) / 2))
    return _deduplicate_positions(positions)


def _deduplicate_positions(positions: Sequence[float]) -> list[float]:
    result: list[float] = []
    for position in sorted(positions):
        if not result or abs(position - result[-1]) > 1e-9 * max(1.0, abs(position)):
            result.append(position)
    return result


def _count_segments(values: np.ndarray) -> int:
    count = 0
    previous_finite = False
    for value in values:
        finite = bool(np.isfinite(value))
        if finite and not previous_finite:
            count += 1
        previous_finite = finite
    return count


def _count_boolean_segments(valid: np.ndarray) -> int:
    count = 0
    previous_valid = False
    for value in valid:
        current = bool(value)
        if current and not previous_valid:
            count += 1
        previous_valid = current
    return count


def _configure_axes(
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


def _configure_parametric_axes(
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
        f"Parametric curve, {parameter} in [{_format_position(t_min)}, {_format_position(t_max)}]"
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


def _set_surface_limits(axes: Axes3D, values: np.ndarray) -> None:
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return
    low = float(np.percentile(finite, 1))
    high = float(np.percentile(finite, 99))
    if high - low <= 1e-12:
        low -= 1.0
        high += 1.0
    axes.set_zlim(low, high)


def _save_png(figure: Figure) -> bytes:
    try:
        figure.tight_layout()
    except (ValueError, RuntimeError):
        pass
    for dpi in (100, 84, 68):
        buffer = io.BytesIO()
        figure.savefig(
            buffer,
            format="png",
            dpi=dpi,
            facecolor="white",
            metadata={"Software": "math-tools"},
        )
        png = buffer.getvalue()
        if len(png) <= MAX_PNG_BYTES:
            return png
    raise MathError("resource_limit", "Не удалось уложить PNG в 256 КиБ.")


def _short_label(text: str) -> str:
    return text if len(text) <= ELLIPSIS_LENGTH else text[: ELLIPSIS_LENGTH - 1] + "…"


def _format_position(value: float) -> str:
    if abs(value) < 1e-12:
        value = 0.0
    return f"{value:.6g}"


def _exact_float(value: float) -> Expr:
    if float(value).is_integer():
        return sympy.Integer(int(value))
    return sympy.Float(value)


def _curve_checkpoints(
    functions: Sequence[tuple[str, ParsedExpression]],
    symbol: Symbol,
    x_min: float,
    x_max: float,
) -> tuple[MathPlotCheckpoint, ...]:
    point = 0.0 if x_min <= 0 <= x_max else (x_min + x_max) / 2
    checkpoints: list[MathPlotCheckpoint] = []
    for _, parsed in functions:
        checkpoint = _real_checkpoint(
            parsed,
            {symbol: _exact_float(point)},
            f"x={_format_position(point)}",
        )
        checkpoints.append(checkpoint)
    return tuple(checkpoints)


def _parametric_checkpoint(
    x_parsed: ParsedExpression,
    y_parsed: ParsedExpression,
    symbol: Symbol,
    t_min: float,
    t_max: float,
) -> MathPlotCheckpoint:
    point = 0.0 if t_min <= 0 <= t_max else (t_min + t_max) / 2
    substitution = {symbol: _exact_float(point)}
    x_value = _real_value(x_parsed.expression, substitution)
    y_value = _real_value(y_parsed.expression, substitution)
    if x_value is None or y_value is None:
        return MathPlotCheckpoint(
            point=f"{symbol.name}={_format_position(point)}",
            value="undefined",
            numeric="",
        )
    return MathPlotCheckpoint(
        point=f"{symbol.name}={_format_position(point)}",
        value=f"({sympy.sstr(x_value)}, {sympy.sstr(y_value)})",
        numeric=f"({_numeric_text(x_value)}, {_numeric_text(y_value)})",
    )


def _surface_checkpoints(
    parsed: ParsedExpression,
    symbols: Sequence[Symbol],
    x_min: float,
    x_max: float,
    y_min: float,
    y_max: float,
) -> tuple[MathPlotCheckpoint, ...]:
    candidates = [
        (0.0, 0.0),
        (1.0, 1.0),
        (-1.0, 1.0),
        (1.0, -1.0),
        (-1.0, -1.0),
    ]
    checkpoints: list[MathPlotCheckpoint] = []
    for x_value, y_value in candidates:
        if not (x_min <= x_value <= x_max and y_min <= y_value <= y_max):
            continue
        substitutions = {
            symbols[0]: _exact_float(x_value),
            symbols[1]: _exact_float(y_value),
        }
        checkpoint = _real_checkpoint(
            parsed,
            substitutions,
            f"(x, y)=({_format_position(x_value)}, {_format_position(y_value)})",
        )
        checkpoints.append(checkpoint)
        if len(checkpoints) >= 4:
            break
    return tuple(checkpoints)


def _complex_checkpoints(
    parsed: ParsedExpression,
    symbol: Symbol,
    x_min: float,
    x_max: float,
    y_min: float,
    y_max: float,
    *,
    include_height: bool,
) -> tuple[MathPlotCheckpoint, ...]:
    candidates = [
        sympy.Integer(0),
        sympy.Integer(1),
        sympy.I,
        sympy.Integer(-1),
        -sympy.I,
        sympy.Integer(2),
        sympy.Integer(-2),
    ]
    checkpoints: list[MathPlotCheckpoint] = []
    for point in candidates:
        numeric_point = _finite_number(point)
        if numeric_point is None:
            continue
        if not (x_min <= numeric_point.real <= x_max and y_min <= numeric_point.imag <= y_max):
            continue
        checkpoint = _complex_checkpoint(parsed, symbol, point, include_height=include_height)
        checkpoints.append(checkpoint)
    return tuple(checkpoints)


def _complex_checkpoint(
    parsed: ParsedExpression,
    symbol: Symbol,
    point: Expr,
    *,
    include_height: bool,
) -> MathPlotCheckpoint:
    label = f"z={sympy.sstr(point)}"
    substitution = {symbol: point}
    try:
        original_value = parsed.original.subs(list(substitution.items()))
    except (TypeError, ValueError):
        original_value = sympy.nan
    if original_value is sympy.zoo or original_value.has(sympy.zoo):
        if _is_log_branch_point(parsed.expression, symbol, point):
            return MathPlotCheckpoint(
                point=label,
                value="branch_point",
                numeric="",
                state="branch_point",
            )
        return MathPlotCheckpoint(point=label, value="pole", numeric="", state="pole")
    if original_value is sympy.nan or original_value.has(sympy.nan):
        return MathPlotCheckpoint(point=label, value="pole", numeric="", state="removable_pole")

    try:
        value = sympy.simplify(parsed.expression.subs(list(substitution.items())))
    except (TypeError, ValueError):
        return MathPlotCheckpoint(point=label, value="undefined", numeric="", state="undefined")
    if value is sympy.zoo or value.has(sympy.zoo):
        return MathPlotCheckpoint(point=label, value="pole", numeric="", state="pole")
    if value is sympy.nan or value.has(sympy.nan, sympy.oo, -sympy.oo):
        return MathPlotCheckpoint(point=label, value="undefined", numeric="", state="undefined")

    numeric = _numeric_text(value)
    if not numeric:
        return MathPlotCheckpoint(point=label, value="undefined", numeric="", state="undefined")

    state = "branch_cut" if _on_branch_cut(parsed.expression, symbol, point) else ""
    if _denominator_violated(parsed.constraints, substitution):
        state = "removable_pole"

    height = ""
    numeric_height = ""
    if include_height:
        height_value = sympy.simplify(sympy.Abs(value))
        height = sympy.sstr(height_value)
        numeric_height = _numeric_text(height_value)
    return MathPlotCheckpoint(
        point=label,
        value=sympy.sstr(value),
        numeric=numeric,
        height=height,
        numeric_height=numeric_height,
        state=state,
    )


def _on_branch_cut(expression: Expr, symbol: Symbol, point: Expr) -> bool:
    for logarithm in expression.atoms(sympy.log):
        if not isinstance(logarithm, sympy.log):
            continue
        argument = logarithm.args[0]
        if not argument.has(symbol):
            continue
        try:
            value = complex(sympy.N(argument.subs(symbol, point), 15))
        except (TypeError, ValueError, OverflowError):
            continue
        if value.real < 0 and abs(value.imag) <= 1e-12 * max(1.0, abs(value.real)):
            return True
    return False


def _is_log_branch_point(expression: Expr, symbol: Symbol, point: Expr) -> bool:
    for logarithm in expression.atoms(sympy.log):
        if not isinstance(logarithm, sympy.log):
            continue
        argument = logarithm.args[0]
        if not argument.has(symbol):
            continue
        try:
            value = sympy.simplify(argument.subs(symbol, point))
        except (TypeError, ValueError):
            continue
        if value.is_zero is True:
            return True
    return False


def _denominator_violated(
    constraints: Sequence[DomainConstraint],
    substitution: Mapping[Symbol, Expr],
) -> bool:
    for constraint in constraints:
        if constraint.relation is not DomainRelation.NONZERO:
            continue
        try:
            raw = complex(sympy.N(constraint.expression.subs(list(substitution.items())), 15))
        except (TypeError, ValueError, OverflowError):
            return True
        if not (math.isfinite(raw.real) and math.isfinite(raw.imag)):
            return True
        if abs(raw) == 0:
            return True
    return False


def _constraint_violated(
    constraints: Sequence[DomainConstraint],
    substitution: Mapping[Symbol, Expr],
) -> bool:
    for constraint in constraints:
        try:
            raw = complex(sympy.N(constraint.expression.subs(list(substitution.items())), 15))
        except (TypeError, ValueError, OverflowError):
            return True
        if not (math.isfinite(raw.real) and math.isfinite(raw.imag)):
            return True
        if abs(raw.imag) > 1e-12 * max(1.0, abs(raw.real)):
            return True
        value = raw.real
        relation = constraint.relation
        if relation is DomainRelation.NONZERO:
            if value == 0:
                return True
        elif relation is DomainRelation.POSITIVE:
            if value <= 0:
                return True
        elif relation is DomainRelation.NONNEGATIVE:
            if value < 0:
                return True
        elif relation is DomainRelation.NEGATIVE:
            if value >= 0:
                return True
        elif relation is DomainRelation.NONPOSITIVE:
            if value > 0:
                return True
    return False


def _real_checkpoint(
    parsed: ParsedExpression,
    substitutions: Mapping[Symbol, Expr],
    label: str,
) -> MathPlotCheckpoint:
    try:
        original_value = parsed.original.subs(list(substitutions.items()))
    except (TypeError, ValueError):
        original_value = sympy.nan
    if original_value is sympy.zoo or original_value.has(sympy.zoo):
        return MathPlotCheckpoint(point=label, value="pole", numeric="", state="pole")
    if original_value is sympy.nan or original_value.has(sympy.nan):
        return MathPlotCheckpoint(point=label, value="pole", numeric="", state="removable_pole")

    value = _real_value(parsed.expression, substitutions)
    if value is None:
        return MathPlotCheckpoint(point=label, value="undefined", numeric="", state="undefined")
    state = "undefined" if _constraint_violated(parsed.constraints, substitutions) else ""
    if _denominator_violated(parsed.constraints, substitutions):
        state = "removable_pole"
    return MathPlotCheckpoint(
        point=label,
        value=sympy.sstr(value),
        numeric=_numeric_text(value),
        state=state,
    )


def _real_value(
    expression: Expr,
    substitutions: Mapping[Symbol, Expr],
) -> Expr | None:
    try:
        value = sympy.simplify(expression.subs(list(substitutions.items())))
    except (TypeError, ValueError):
        return None
    if value is sympy.zoo or value.has(sympy.zoo, sympy.nan, sympy.oo, -sympy.oo):
        return None
    if value.free_symbols:
        return None
    numeric = _finite_number(value)
    if numeric is None or abs(numeric.imag) > 1e-12 * max(1.0, abs(numeric.real)):
        return None
    return value


def _numeric_text(value: Expr) -> str:
    try:
        numeric = sympy.N(value, 15)
    except (TypeError, ValueError):
        return ""
    if numeric is sympy.zoo or numeric.has(sympy.zoo, sympy.nan, sympy.oo, -sympy.oo):
        return ""
    return sympy.sstr(numeric)
