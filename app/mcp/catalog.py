import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.protocols import Validator
from mcp import types

from app.core.schemas.llm import ToolDefinition
from app.core.schemas.tools import ToolFailure


@dataclass(frozen=True)
class DiscoveredTool:
    definition: ToolDefinition
    read_only: bool
    input_validator: Validator
    output_validator: Validator


class ToolCatalog:
    def __init__(self) -> None:
        self._entries: dict[str, DiscoveredTool] = {}

    def load(self, discovered: Sequence[types.Tool]) -> dict[str, ToolDefinition]:
        entries: dict[str, DiscoveredTool] = {}

        for tool in discovered:
            if tool.output_schema is None:
                continue

            Draft202012Validator.check_schema(tool.input_schema)
            Draft202012Validator.check_schema(tool.output_schema)
            entries[tool.name] = DiscoveredTool(
                definition=ToolDefinition(
                    name=tool.name,
                    description=tool.description or "",
                    parameters=tool.input_schema,
                ),
                read_only=(
                    tool.annotations is not None and tool.annotations.read_only_hint is True
                ),
                input_validator=Draft202012Validator(
                    tool.input_schema,
                    format_checker=FormatChecker(),
                ),
                output_validator=Draft202012Validator(
                    tool.output_schema,
                    format_checker=FormatChecker(),
                ),
            )

        self._entries = entries
        return {name: entry.definition for name, entry in entries.items()}

    def is_read_only(self, name: str) -> bool:
        entry = self._entries.get(name)
        return entry is not None and entry.read_only

    def validate_arguments(self, name: str, arguments: Mapping[str, object]) -> None:
        entry = self._entries.get(name)
        if entry is None:
            raise ToolFailure("unknown_tool", "Инструмент не разрешён или недоступен.")

        try:
            entry.input_validator.validate(arguments)
        except Exception:
            raise ToolFailure("invalid_arguments", "Уточните параметры инструмента.") from None

    def validate_output(self, name: str, payload: object) -> dict[str, object]:
        entry = self._entries.get(name)
        try:
            if entry is None or not isinstance(payload, dict) or "error" in payload:
                raise ValueError("missing_success_payload")
            json.dumps(payload, allow_nan=False)
            entry.output_validator.validate(payload)
        except Exception:
            raise ToolFailure(
                "invalid_output", "Инструмент вернул некорректный результат."
            ) from None
        return payload
