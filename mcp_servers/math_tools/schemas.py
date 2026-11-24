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


class LimitDirection(StrEnum):
    BOTH = "both"
    LEFT = "left"
    RIGHT = "right"


class CalculateStatus(StrEnum):
    OK = "ok"
    UNDEFINED = "undefined"


class SolveStatus(StrEnum):
    SOLUTIONS = "solutions"
    NO_SOLUTION = "no_solution"
    INFINITE_SOLUTIONS = "infinite_solutions"
    UNDEFINED = "undefined"


class PlotStatus(StrEnum):
    OK = "ok"
    UNDEFINED = "undefined"


class ToolDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MathCalculateResult(ToolDTO):
    status: CalculateStatus
    operation: CalculateOperation
    expression: str
    variable: str
    result: str
    numeric: str
    exact: bool
    detail: str


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


class MathPlotResult(ToolDTO):
    status: PlotStatus = Field(title="Status")
    functions: list[MathPlotSeries] = Field(min_length=1)
    variable: str
    x_min: float = Field(allow_inf_nan=False)
    x_max: float = Field(allow_inf_nan=False)
    samples: int = Field(gt=0)
    png_bytes: int = Field(ge=0, le=MAX_PNG_BYTES)
    detail: str


@dataclass(frozen=True)
class PlotOutput:
    result: MathPlotResult
    png: bytes
