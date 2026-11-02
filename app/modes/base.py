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

    def build_initial_messages(
        self,
        user_text: str | None = None,
    ) -> list[ChatMessage]:
        messages: list[ChatMessage] = [
            ChatMessage(role=MessageRole.SYSTEM, content=self.system_prompt)
        ]

        example = self._select_example(user_text)
        if example is None:
            return messages

        example_size = len(example.user_input) + len(example.assistant_output)
        example_budget = 1800
        if example_size > example_budget:
            return messages

        messages.append(ChatMessage(role=MessageRole.USER, content=example.user_input))
        messages.append(ChatMessage(role=MessageRole.ASSISTANT, content=example.assistant_output))

        return messages

    def _select_example(self, user_text: str | None) -> FewShotExample | None:
        if not self.few_shot_examples:
            return None

        if user_text is None:
            return self.few_shot_examples[0]

        query = user_text.casefold()
        selected: FewShotExample | None = None
        best_score = 0

        for example in self.few_shot_examples:
            score = sum(keyword in query for keyword in example.keywords)
            if example.keywords and score == 0:
                continue

            if selected is None or score > best_score:
                selected = example
                best_score = score

        return selected
