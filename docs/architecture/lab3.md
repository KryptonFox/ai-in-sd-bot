# Архитектура Lab 3

```mermaid
flowchart LR
    Telegram --> DialogService
    Telegram --> CalendarImport["Импорт iCalendar"]
    CalendarImport --> PostgreSQL
    DialogService --> Supervisor
    Supervisor --> Router["Decisions API"]
    Supervisor --> Schedule
    Supervisor --> Knowledge
    Supervisor --> Lifestyle
    Supervisor --> Math
    Supervisor --> Legacy
    Schedule --> StudentMCP
    Knowledge --> StudentMCP
    Lifestyle --> StudentMCP
    Math --> MathMCP
    StudentMCP --> PostgreSQL
    DialogService --> PostgreSQL
```

DialogService управляет очередью владельца, повтором результата и общим
дедлайном. Решение Router помогает выбрать исполнителя. Supervisor ограничивает
меню специалиста по текущему MCP discovery. Перед вызовом приложение проверяет
весь пакет инструментов. Student MCP получает владельца календаря и разрешение
веб-поиска в метаданных клиента. Подтверждение напоминания привязано к сохранённому
действию и владельцу.

```mermaid
sequenceDiagram
    participant N as Сервис уведомлений
    participant DB as PostgreSQL
    participant S as Источники сводки
    participant T as Telegram
    N->>DB: Подготовить задания и захватить запись с арендой
    opt Утренняя сводка
        N->>S: Расписание, напоминания, погода, учебный фокус
        S-->>N: Доступные разделы и ошибки отдельных источников
        N->>DB: Сохранить подготовленный текст
    end
    N->>DB: Проверить окно, адрес и согласие
    N->>T: Отправить в доверенный личный чат
    N->>DB: Зафиксировать состояние попытки
```

Миграции разделяют личные календари, настройки и очередь уведомлений, технические
трассы. Идемпотентный ключ защищает создание задания. Сбой после ответа Telegram
и до записи результата оставляет возможность повторной доставки.
