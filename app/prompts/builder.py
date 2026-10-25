from pydantic import BaseModel, ConfigDict, Field

from app.prompts.policy import AUDIENCE_CONTEXT, SAFETY_TASK, TRUST_CONTEXT
from app.prompts.telegram import TELEGRAM_FORMATTING_PROMPT


class PromptParts(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    role: str = Field(min_length=1)
    context: str = Field(min_length=1)
    task: str = Field(min_length=1)
    format: str = Field(min_length=1)


def build_prompt(parts: PromptParts, *, telegram: bool = True) -> str:
    context = f"{parts.context}\n\n{TRUST_CONTEXT}"
    if telegram:
        context += f"\n\n{AUDIENCE_CONTEXT}"
    task = f"{parts.task}\n\n{SAFETY_TASK}"
    output_format = parts.format
    if telegram:
        output_format += f"\n\n{TELEGRAM_FORMATTING_PROMPT}"
    return (
        f"Role:\n{parts.role}\n\nContext:\n{context}\n\nTask:\n{task}\n\nFormat:\n{output_format}"
    )
