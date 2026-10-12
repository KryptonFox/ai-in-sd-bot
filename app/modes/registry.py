from app.modes.base import BotMode
from app.modes.definitions import REVIEW_MODE, STUDY_MODE, TRANSLATE_MODE


class ModeRegistry:
    def __init__(self, default_id: str = "study") -> None:
        self._modes: dict[str, BotMode] = {}
        self._commands_map: dict[str, str] = {}
        self._default_id = default_id

    def register(self, mode: BotMode) -> None:
        self._modes[mode.id] = mode
        self._commands_map[mode.command] = mode.id

    def get(self, identifier: str) -> BotMode | None:
        if identifier in self._modes:
            return self._modes[identifier]

        mode_id = self._commands_map.get(identifier)
        return self._modes.get(mode_id) if mode_id else None

    def get_or_default(self, identifier: str) -> BotMode:
        mode = self.get(identifier)
        if mode is not None:
            return mode

        return self._modes[self._default_id]

    def list_modes(self) -> list[BotMode]:
        return list(self._modes.values())

    def commands(self) -> list[str]:
        return list(self._commands_map.keys())

    def format_help(self) -> str:
        lines = ["🤖 **Доступные режимы ассистента:**"]

        for mode in self.list_modes():
            lines.append(f"• `/{mode.command}` — **{mode.name}**: {mode.description}")

        return "\n".join(lines)


registry = ModeRegistry(default_id="study")
registry.register(STUDY_MODE)
registry.register(TRANSLATE_MODE)
registry.register(REVIEW_MODE)
