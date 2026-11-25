from enum import StrEnum


class WorkerOperation(StrEnum):
    CALCULATE = "calculate"
    SOLVE = "solve"
    PLOT = "plot"
    WARMUP = "warmup"
    LIMITS = "limits"
