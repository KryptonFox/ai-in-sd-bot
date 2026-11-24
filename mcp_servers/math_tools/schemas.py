from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from mcp_servers.math_tools.limits import MAX_PNG_BYTES


class CalculateOperation(StrEnum):
    SIMPLIFY = "simplify"
    EVALUATE = "evaluate"
    NUMERIC = "numeric"
    DIFF = "diff"
    INTEGRATE = "integrate"
    LIMIT = "limit"
    PARTIAL_DIFF = "partial_diff"
    JACOBIAN = "jacobian"
    CONSTRAINED_EXTREMA = "constrained_extrema"
    SERIES = "series"


class LimitDirection(StrEnum):
    BOTH = "both"
    LEFT = "left"
    RIGHT = "right"


class CalculateStatus(StrEnum):
    OK = "ok"
    UNDEFINED = "undefined"
    NO_SOLUTION = "no_solution"


class ConvergenceStatus(StrEnum):
    CONVERGENT = "convergent"
    DIVERGENT = "divergent"
    UNKNOWN = "unknown"


class ExtremaKind(StrEnum):
    MINIMUM = "minimum"
    MAXIMUM = "maximum"
    STATIONARY = "stationary"


class SolveStatus(StrEnum):
    SOLUTIONS = "solutions"
    NO_SOLUTION = "no_solution"
    INFINITE_SOLUTIONS = "infinite_solutions"
    UNDEFINED = "undefined"


class PlotStatus(StrEnum):
    OK = "ok"
    UNDEFINED = "undefined"


class PlotType(StrEnum):
    CURVE = "curve"
    PARAMETRIC = "parametric"
    CONTOUR = "contour"
    DOMAIN_COLORING = "domain_coloring"
    SURFACE = "surface"
    COMPLEX_HEIGHT = "complex_height"


class ToolDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MathPartial(ToolDTO):
    variable: str
    order: int = Field(ge=1)
    exact: str
    numeric: str


class MathExtremum(ToolDTO):
    point: dict[str, str]
    numeric_point: dict[str, str]
    value: str
    numeric_value: str
    multiplier: str
    numeric_multiplier: str
    kind: ExtremaKind


class MathCalculateResult(ToolDTO):
    status: CalculateStatus
    operation: CalculateOperation
    expression: str
    variable: str
    result: str
    numeric: str
    exact: bool
    detail: str
    variables: list[str] = Field(default_factory=list)
    constraint: str = ""
    convergence: ConvergenceStatus | None = None
    absolute_convergence: ConvergenceStatus | None = None
    conditional: bool | None = None
    partials: list[MathPartial] = Field(default_factory=list)
    jacobian: list[list[str]] = Field(default_factory=list)
    jacobian_numeric: list[list[str]] = Field(default_factory=list)
    extrema: list[MathExtremum] = Field(default_factory=list)


class MathSolveResult(ToolDTO):
    status: SolveStatus
    equations: list[str]
    variables: list[str]
    solutions: list[dict[str, str]]
    numeric_solutions: list[dict[str, str]]
    discarded: list[dict[str, str]]
    real_only: bool
    verified: bool
    detail: str


class MathPlotSeries(ToolDTO):
    expression: str
    segments: int = Field(ge=0)
    discontinuities: list[str]


class MathPlotCheckpoint(ToolDTO):
    point: str
    value: str
    numeric: str
    height: str = ""
    numeric_height: str = ""
    state: str = ""


class MathPlotZero(ToolDTO):
    re: float = Field(allow_inf_nan=False)
    im: float = Field(allow_inf_nan=False)


class MathPlotResult(ToolDTO):
    status: PlotStatus = Field(title="Status")
    plot_type: PlotType = PlotType.CURVE
    functions: list[MathPlotSeries] = Field(min_length=1)
    variable: str
    x_min: float = Field(allow_inf_nan=False)
    x_max: float = Field(allow_inf_nan=False)
    y_min: float | None = Field(default=None, allow_inf_nan=False)
    y_max: float | None = Field(default=None, allow_inf_nan=False)
    samples: int = Field(gt=0)
    png_bytes: int = Field(ge=0, le=MAX_PNG_BYTES)
    masked_points: int = Field(default=0, ge=0)
    total_points: int = Field(default=0, ge=0)
    branch_cuts: list[str] = Field(default_factory=list)
    poles: list[str] = Field(default_factory=list)
    checkpoints: list[MathPlotCheckpoint] = Field(default_factory=list)
    zero_markers: list[MathPlotZero] = Field(default_factory=list)
    levels: list[float] = Field(default_factory=list)
    color_scale: str = ""
    detail: str


@dataclass(frozen=True)
class PlotOutput:
    result: MathPlotResult
    png: bytes
