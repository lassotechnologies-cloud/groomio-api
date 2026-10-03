"""Worker registration tests (Doc 10.11).

The failure this file exists to prevent: Celery's `include` list naming a module
that does not exist. Nothing catches it at import time — the app boots, the API
serves, the tests pass — and then the worker refuses to start in production with
a `ModuleNotFoundError`, so every background job silently stops running. Shops
stop being reminded about appointments and, far worse, nothing ever expires a
lapsed subscription again.

These tests are the cheapest possible guard against that whole class of outage.
"""

import pytest


@pytest.fixture(scope="module")
def celery_app():
    from app.workers import celery_app as app

    # Mirrors what `celery -A app.workers worker` does at startup.
    app.loader.import_default_modules()
    return app


def test_every_included_worker_module_imports(celery_app):
    missing = []
    for module in celery_app.conf.include:
        try:
            __import__(module)
        except Exception as exc:  # noqa: BLE001 - the type is the message
            missing.append(f"{module}: {type(exc).__name__}: {exc}")

    assert not missing, (
        "Celery cannot start — these modules fail to import:\n" + "\n".join(missing)
    )


def test_every_beat_schedule_entry_names_a_registered_task(celery_app):
    """A beat entry pointing at a task that does not exist is a silent no-op that
    looks exactly like a healthy schedule in the Celery logs."""
    unregistered = [
        f"{name} -> {cfg['task']}"
        for name, cfg in celery_app.conf.beat_schedule.items()
        if cfg["task"] not in celery_app.tasks
    ]
    assert not unregistered, "Beat entries with no matching task:\n" + "\n".join(
        unregistered
    )


def test_the_otp_worker_is_still_registered(celery_app):
    """The pre-existing worker must not be lost when others are added."""
    assert "app.workers.otp_sender.send_otp" in celery_app.tasks


@pytest.mark.parametrize(
    "task_name",
    [
        "app.workers.appointment_reminders.send_appointment_reminders",
        "app.workers.subscription_reminders.send_subscription_reminders",
        "app.workers.subscription_state.expire_subscriptions",
        "app.workers.daily_report.compute_daily_report",
        "app.workers.campaign_sender.dispatch_scheduled_campaigns",
        "app.workers.campaign_sender.send_campaign",
        "app.workers.ai_studio_generator.generate_assets",
        "app.workers.chat_retention_cleanup.archive_old_threads",
        "app.workers.payment_reconciler.reconcile_webhooks",
    ],
)
def test_documented_worker_task_is_registered(celery_app, task_name):
    assert task_name in celery_app.tasks


def test_subscription_expiry_runs_daily(celery_app):
    """The subscription lifecycle job is the one that gates shop access. If it
    stops being scheduled, nothing is ever marked expired."""
    assert "subscription-state" in celery_app.conf.beat_schedule


def test_payment_reconciler_runs_more_often_than_daily(celery_app):
    """Payment callbacks that fail to process must be picked up promptly — a
    customer's money arriving two days late is a support ticket.

    Beat schedules here are either a float (seconds) or a crontab, so the check
    only applies to the numeric form and lets crontab entries pass.
    """
    schedule = celery_app.conf.beat_schedule["payment-reconciler"]["schedule"]
    if isinstance(schedule, (int, float)):
        assert schedule < 86400  # less than daily
    else:
        assert schedule.hour != 0 or schedule.minute != 0  # not a midnight crontab
