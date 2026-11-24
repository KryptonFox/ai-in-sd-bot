from app.core.schemas.message import ChatMessage, MessageRole


def complete_unit_indices(messages: list[ChatMessage]) -> list[list[int]]:
    """Group complete dialog turns without splitting assistant tool calls from results."""
    units: list[list[int]] = []
    current: list[int] = []

    for index, message in enumerate(messages):
        if message.role == MessageRole.SYSTEM:
            continue

        if message.role == MessageRole.USER:
            current = [index]
            continue

        if not current:
            continue

        if message.role == MessageRole.TOOL:
            current.append(index)
            continue

        if message.role == MessageRole.ASSISTANT:
            current.append(index)
            if not message.tool_calls:
                units.append(current)
                current = []

    return units


def complete_pairs(messages: list[ChatMessage]) -> list[list[ChatMessage]]:
    return [[messages[index] for index in unit] for unit in complete_unit_indices(messages)]
