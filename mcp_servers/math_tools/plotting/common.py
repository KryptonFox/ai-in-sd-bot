import io
import math
from dataclasses import dataclass

import numpy as np
import sympy
from matplotlib.figure import Figure
from sympy import Expr

from mcp_servers.math_tools.errors import MathError
from mcp_servers.math_tools.limits import MAX_PNG_BYTES
from mcp_servers.math_tools.schemas import MathPlotCheckpoint, MathPlotSeries

COLORS = ("#1f77b4", "#d62728", "#2ca02c", "#9467bd")
ELLIPSIS_LENGTH = 48
BRANCH_CUT_WIDTH_CELLS = 0.75
MAX_LISTED_POLES = 8
PNG_DPI_STEPS = (100, 84, 68)


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
class ComplexField:
    values: np.ndarray
    mask: np.ndarray
    grid_x: np.ndarray
    grid_y: np.ndarray
    branch_cuts: tuple[str, ...]
    poles: tuple[str, ...]


def save_png(figure: Figure, max_png_bytes: int = MAX_PNG_BYTES) -> bytes:
    limit = _png_limit(max_png_bytes)
    try:
        figure.tight_layout()
    except (ValueError, RuntimeError):
        pass
    for dpi in PNG_DPI_STEPS:
        buffer = io.BytesIO()
        figure.savefig(
            buffer,
            format="png",
            dpi=dpi,
            facecolor="white",
            metadata={"Software": "math-tools"},
        )
        png = buffer.getvalue()
        if len(png) <= limit:
            return png
    raise MathError("resource_limit", f"Не удалось уложить PNG в {limit} байт.")


def _png_limit(value: int) -> int:
    return min(max(int(value), 1), MAX_PNG_BYTES)


def short_label(text: str) -> str:
    return text if len(text) <= ELLIPSIS_LENGTH else text[: ELLIPSIS_LENGTH - 1] + "…"


def format_position(value: float) -> str:
    if abs(value) < 1e-12:
        value = 0.0
    return f"{value:.6g}"


def exact_float(value: float) -> Expr:
    if float(value).is_integer():
        return sympy.Integer(int(value))
    return sympy.Float(value)


def finite_number(value: Expr) -> complex | None:
    try:
        numeric = complex(sympy.N(value, 15))
    except (TypeError, ValueError, OverflowError):
        return None
    if not (math.isfinite(numeric.real) and math.isfinite(numeric.imag)):
        return None
    return numeric
