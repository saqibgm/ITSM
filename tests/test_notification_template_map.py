"""Regression test: every NotificationType must have an explicit email
template mapping — a missing entry silently falls back to a generic template,
which is the exact risk flagged when RCA/recording types were added (specs/08)."""
from app.models.notification import NotificationType
from app.services.notification_service import EMAIL_TEMPLATE_MAP


def test_every_notification_type_has_a_template_entry():
    missing = [t for t in NotificationType if t not in EMAIL_TEMPLATE_MAP]
    assert not missing, f"NotificationType values missing from EMAIL_TEMPLATE_MAP: {missing}"
