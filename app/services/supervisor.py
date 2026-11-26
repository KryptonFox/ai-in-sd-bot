import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from app.core.schemas.message import ChatMessage, MessageRole
from app.core.schemas.routing import RouteDecision, RouteTarget
from app.services.agent_service import AgentService

_WEB_NEGATION = re.compile(
    r"(?:\bне\s+(?:ищи|надо\s+искать|нужно\s+искать|используй\s+веб|подключай\s+веб|"
    r"проверяй\s+в\s+интернете)\b|\bбез\s+(?:веб-поиск\w*|интернет\w*|сети)\b|"
    r"\bтолько\s+(?:по\s+)?(?:лекци\w*|материал\w*|корпус\w*)\b)",
    re.IGNORECASE,
)
_WEB_REQUEST = re.compile(
    r"\b(?:найди(?:те)?|найти|поищи(?:те)?|поискать|ищи(?:те)?|"
    r"посмотри(?:те)?|посмотреть|проверь(?:те)?|проверить|"
    r"сверь(?:те)?|сверить|сравни(?:те)?|сравнить|"
    r"сделай(?:те)?|выполни(?:те)?|используй(?:те)?|подключи(?:те)?|запусти(?:те)?)\b"
    r"[^.!?\n]{0,250}?"
    r"(?:"
    r"\b(?:в\s+интернете|в\s+сети|в\s+вебе|онлайн|из\s+интернета|с\s+интернетом|"
    r"через\s+веб-поиск\w*|веб-поиском|"
    r"с\s+веб-источник\w*|в\s+веб-источник\w*|из\s+веб-источник\w*|веб-источник\w*|"
    r"официальн\w+\s+документаци\w*)\b"
    r"|\b(?:сделай(?:те)?|выполни(?:те)?|используй(?:те)?|подключи(?:те)?|запусти(?:те)?)\s+"
    r"веб-поиск\w*\b"
    r")",
    re.IGNORECASE,
)
_REMINDER_NEGATION = re.compile(
    r"(?:^\s*(?:отмена|отмени(?:те)?|не\s+надо|не\s+нужно)[.!?\s]*$|"
    r"\bне\s+(?:напоминай(?:те)?|ставь(?:те)?\s+напоминани|добавляй(?:те)?\s+напоминани|"
    r"создавай(?:те)?\s+напоминани|нужно\s+напоминани|надо\s+напоминани)\b|"
    r"\b(?:отмени(?:те)?|удали(?:те)?)\s+напоминани\w*\b)",
    re.IGNORECASE,
)
_REMINDER_REQUEST = re.compile(
    r"(?:^|[.!?]\s+|\b(?:пожалуйста|можешь|можете|нужно|надо|хочу|и|а\s+ещё|ещё)[, ]+)"
    r"(?:напомни(?:те)?\b|напомнить\b|"
    r"(?:добавь(?:те)?|добавить|создай(?:те)?|создать|поставь(?:те)?|поставить|"
    r"установи(?:те)?|установить|запиши(?:те)?|записать|сделай(?:те)?|сделать)\b"
    r"[^.!?\n]{0,70}\bнапоминани[ея]\b)",
    re.IGNORECASE,
)
_SCHEDULE_READ_QUERY = re.compile(
    r"^\s*(?:покажи(?:те)?|выведи(?:те)?|скажи(?:те)?|какое|какие|что\s+у\s+меня|есть\s+ли)\b"
    r"[^.!?\n]{0,80}\b(?:расписани\w*|пар[аы]?|заняти\w*|окн[ао]|свободн\w+\s+дн\w*)\b",
    re.IGNORECASE,
)
_WEB_APPROVAL = frozenset(
    {
        "да",
        "да, поищи",
        "да, давай",
        "да, пожалуйста",
        "да, в интернете",
        "поищи",
        "поищи в интернете",
        "поискать",
        "давай",
        "согласен",
        "согласна",
        "хорошо",
        "ок",
        "окей",
        "конечно",
    }
)


class Router(Protocol):
    async def route(self, text: str, history: Sequence[ChatMessage]) -> RouteDecision: ...


@dataclass(frozen=True)
class RouteSelection:
    target: RouteTarget
    agent: AgentService | None
    web_allowed: bool = False
    reminder_allowed: bool = False
    router_invoked: bool = True


class Supervisor:
    def __init__(
        self, router: Router, agents: Mapping[RouteTarget, AgentService], model: str
    ) -> None:
        self._router = router
        self._agents = dict(agents)
        self.model = model

    async def select(self, text: str, history: Sequence[ChatMessage]) -> RouteSelection:
        web_allowed = self.web_search_requested(text, history)
        if web_allowed and _last_assistant_flag(history, "web_search_offer"):
            target = RouteTarget.KNOWLEDGE
            router_invoked = False
        else:
            decision = await self._router.route(text, history)
            target = self._validated_target(decision)
            router_invoked = True

        agent = None if target is RouteTarget.CLARIFY else self._agents.get(target)
        if target is not RouteTarget.CLARIFY and agent is None:
            target = RouteTarget.CLARIFY

        if target is RouteTarget.CLARIFY:
            return RouteSelection(
                target=RouteTarget.CLARIFY,
                agent=None,
                web_allowed=False,
                reminder_allowed=False,
                router_invoked=router_invoked,
            )

        return RouteSelection(
            target=target,
            agent=agent,
            web_allowed=target is RouteTarget.KNOWLEDGE and web_allowed,
            reminder_allowed=target is RouteTarget.SCHEDULE
            and self.reminder_requested(text, history),
            router_invoked=router_invoked,
        )

    @staticmethod
    def _validated_target(decision: RouteDecision) -> RouteTarget:
        if not isinstance(decision, RouteDecision):
            return RouteTarget.CLARIFY
        try:
            return RouteTarget(decision.target)
        except ValueError:
            return RouteTarget.CLARIFY

    @staticmethod
    def web_search_requested(text: str, history: Sequence[ChatMessage]) -> bool:
        stripped = text.strip()
        if not stripped or _WEB_NEGATION.search(stripped) is not None:
            return False

        normalized = re.sub(r"[.!?\s]+$", "", stripped.casefold())
        if normalized in _WEB_APPROVAL and _last_assistant_flag(history, "web_search_offer"):
            return True

        return _WEB_REQUEST.search(stripped) is not None

    @staticmethod
    def reminder_requested(text: str, history: Sequence[ChatMessage]) -> bool:
        stripped = text.strip()
        if not stripped or _REMINDER_NEGATION.search(stripped) is not None:
            return False

        if _REMINDER_REQUEST.search(stripped) is not None:
            return True

        if _last_assistant_flag(history, "reminder_followup"):
            return _SCHEDULE_READ_QUERY.search(stripped) is None

        return False


def _last_assistant_flag(history: Sequence[ChatMessage], flag: str) -> bool:
    return bool(
        history
        and history[-1].role is MessageRole.ASSISTANT
        and history[-1].metadata.get(flag) is True
    )
