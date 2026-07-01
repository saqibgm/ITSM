from celery import Celery
from celery.schedules import crontab

from app.config import get_settings


def create_celery() -> Celery:
    s = get_settings()
    app = Celery("itsm", broker=s.CELERY_BROKER_URL, backend=s.CELERY_RESULT_BACKEND)
    app.conf.task_routes = {
        "app.workers.tasks_notifications.*": {"queue": "high"},
        "app.workers.tasks_sla.*": {"queue": "high"},
        "app.workers.tasks_ai_ticket.*": {"queue": "low"},
        "app.workers.tasks_ai_kb.*": {"queue": "low"},
        "app.workers.tasks_kb.*": {"queue": "low"},
        "app.workers.tasks_ai_assets.*": {"queue": "low"},
        "app.workers.tasks_iam_sync.*": {"queue": "low"},
        "app.workers.tasks_ai_budget.*": {"queue": "low"},
        "app.workers.tasks_webhooks.*": {"queue": "default"},
        "app.workers.tasks_automation.*": {"queue": "default"},
    }
    app.conf.task_serializer = "json"
    app.conf.result_serializer = "json"
    app.conf.accept_content = ["json"]
    app.conf.timezone = "UTC"
    app.conf.beat_schedule = {
        "sla-breach-check": {
            "task": "app.workers.tasks_sla.check_sla_breaches",
            "schedule": 300.0,  # every 5 minutes
        },
        "sla-instance-scan": {
            "task": "app.workers.tasks_sla.scan_sla_instances",
            "schedule": 300.0,  # every 5 minutes — warnings + breaches over sla_instances
        },
        "sla-metrics-daily": {
            "task": "app.workers.tasks_sla.flush_sla_metrics_daily",
            "schedule": crontab(hour=2, minute=45),  # nightly rollup
        },
        "iam-user-sync": {
            "task": "app.workers.tasks_iam_sync.sync_all_tenants",
            "schedule": 900.0,  # every 15 minutes
        },
        "flush-ai-daily-usage": {
            "task": "app.workers.tasks_ai_budget.flush_daily_usage",
            "schedule": crontab(hour=2, minute=0),  # 2 am UTC daily
        },
        "run-maintenance-predictions": {
            "task": "app.workers.tasks_ai_assets.run_maintenance_predictions",
            "schedule": crontab(hour=2, minute=30),  # 2:30 am UTC nightly
        },
        "refresh-kb-embeddings": {
            "task": "app.workers.tasks_kb.refresh_kb_embeddings",
            "schedule": crontab(hour=3, minute=0),  # 3:00 am UTC daily
        },
        "auto-draft-kb": {
            "task": "app.workers.tasks_kb.auto_draft_kb_from_tickets",
            "schedule": crontab(hour=3, minute=30),  # 3:30 am UTC daily
        },
        "retry-failed-webhooks": {
            "task": "app.workers.tasks_webhooks.retry_failed_webhooks",
            "schedule": crontab(minute="*/5"),  # every 5 minutes
        },
    }
    return app


celery_app = create_celery()
