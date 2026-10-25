from pydantic import BaseModel, ConfigDict, Field

from app.core.schemas.message import ChatMessage, MessageRole


class FewShotExample(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_input: str
    assistant_output: str
    keywords: list[str] = Field(default_factory=list)


class BotMode(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    command: str
    name: str
    description: str
    system_prompt: str
    few_shot_examples: list[FewShotExample] = Field(default_factory=list)

    def build_initial_messages(self, user_text: str | None = None) -> list[ChatMessage]:
        messages: list[ChatMessage] = [
            ChatMessage(role=MessageRole.SYSTEM, content=self.system_prompt)
        ]

        examples = self.few_shot_examples
        if user_text is not None:
            query = user_text.casefold()
            examples = sorted(
                (ex for ex in examples if not ex.keywords or any(k in query for k in ex.keywords)),
                key=lambda ex: sum(k in query for k in ex.keywords),
                reverse=True,
            )
        budget = 1800
        for example in examples[:1]:
            size = len(example.user_input) + len(example.assistant_output)
            if size > budget:
                continue
            budget -= size
            messages.append(ChatMessage(role=MessageRole.USER, content=example.user_input))
            messages.append(
                ChatMessage(role=MessageRole.ASSISTANT, content=example.assistant_output)
            )

        return messages
