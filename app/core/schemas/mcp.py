from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field


class ReadResourceArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uri: str = Field(min_length=1)


@dataclass(frozen=True)
class ResourceDefinition:
    uri: str
    name: str
    description: str = ""
    mime_type: str = "application/json"


@dataclass(frozen=True)
class ResourceContent:
    uri: str
    text: str
    mime_type: str = "application/json"
