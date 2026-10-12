from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator

VALID_TEMPERATURES: tuple[float, ...] = (0.0, 0.3, 0.7, 1.0)


class UserSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: int
    mode: str = "study"
    temperature: float = 0.7
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @field_validator("temperature")
    @classmethod
    def validate_temperature(cls, v: float) -> float:
        rounded = round(float(v), 1)

        if rounded not in VALID_TEMPERATURES:
            allowed = ", ".join(map(str, VALID_TEMPERATURES))
            raise ValueError(f"Недопустимая температура {v}. Разрешены: {allowed}")

        return rounded
