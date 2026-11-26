from app.db.repositories.notification_delivery_repo import (
    PostgresNotificationDeliveryRepository,
)
from app.db.repositories.notification_preferences_repo import (
    PostgresNotificationPreferencesRepository,
)


class PostgresNotificationRepository(
    PostgresNotificationPreferencesRepository,
    PostgresNotificationDeliveryRepository,
):
    pass


__all__ = ["PostgresNotificationRepository"]
