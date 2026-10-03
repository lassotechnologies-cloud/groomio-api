"""Celery workers — background jobs so the app screen never freezes (Doc 10.11).

Jobs are queued in Redis; workers pick them up one by one.
"""

from celery import Celery
from celery.schedules import crontab

from app.core.config import settings

celery_app = Celery(
    "groomio",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=[
        "app.workers.otp_sender",
        "app.workers.otp_email_sender",
        "app.workers.appointment_reminders",
        "app.workers.subscription_reminders",
        "app.workers.subscription_state",
        "app.workers.daily_report",
        "app.workers.campaign_sender",
        "app.workers.ai_studio_generator",
        "app.workers.chat_retention_cleanup",
        "app.workers.payment_reconciler",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Africa/Nairobi",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    beat_schedule={
        # Doc 10.11 — Celery beat schedule
        # Campaign dispatch: polls for campaigns whose scheduled_at has passed and
        # hands each to send_campaign. Frequent enough that a "3pm blast" lands at
        # 3pm, not at the next polling interval after it.
        "campaign-dispatch": {
            "task": "app.workers.campaign_sender.dispatch_scheduled_campaigns",
            "schedule": 60.0,  # every minute
        },
        "appointment-reminders": {
            "task": "app.workers.appointment_reminders.send_appointment_reminders",
            "schedule": 900.0,  # every 15 min
        },
        "subscription-reminders": {
            "task": "app.workers.subscription_reminders.send_subscription_reminders",
            "schedule": crontab(hour=8, minute=0),  # daily 08:00 EAT
        },
        "subscription-state": {
            "task": "app.workers.subscription_state.expire_subscriptions",
            "schedule": crontab(hour=0, minute=5),  # daily 00:05 EAT
        },
        "daily-report": {
            "task": "app.workers.daily_report.compute_daily_report",
            "schedule": crontab(hour=21, minute=30),  # daily 21:30 EAT
        },
        "chat-retention-cleanup": {
            "task": "app.workers.chat_retention_cleanup.archive_old_threads",
            "schedule": crontab(day_of_week="monday", hour=2, minute=0),  # weekly
        },
        "payment-reconciler": {
            "task": "app.workers.payment_reconciler.reconcile_webhooks",
            "schedule": 300.0,  # every 5 min
        },
    },
)
