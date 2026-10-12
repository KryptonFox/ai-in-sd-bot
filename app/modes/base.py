from pydantic import BaseModel, ConfigDict, Field

from app.core.schemas.message import ChatMessage, MessageRole


class FewShotExample(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_input: str
    assistant_output: str


class BotMode(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    command: str
    name: str
    description: str
    system_prompt: str
    few_shot_examples: list[FewShotExample] = Field(default_factory=list)

    def build_initial_messages(self) -> list[ChatMessage]:
        messages: list[ChatMessage] = [
            ChatMessage(role=MessageRole.SYSTEM, content=self.system_prompt)
        ]

        for example in self.few_shot_examples:
            messages.append(ChatMessage(role=MessageRole.USER, content=example.user_input))
            messages.append(
                ChatMessage(role=MessageRole.ASSISTANT, content=example.assistant_output)
            )

        return messages
