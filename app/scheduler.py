"""Cron-driven update jobs, backed by the schedules table."""

from __future__ import annotations

import os
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import db, dockerops, updates

TIMEZONE = ZoneInfo(os.getenv("TZ", "UTC"))
scheduler = BackgroundScheduler(timezone=TIMEZONE)


def _job_id(schedule_id: int) -> str:
    return f"schedule-{schedule_id}"


def run_schedule(schedule_id: int, actor: str = "scheduler") -> None:
    schedule = db.get_schedule(schedule_id)
    if not schedule:
        return
    targets = dockerops.select_targets(schedule["mode"], schedule["containers"])
    db.log_action(
        actor,
        f"run '{schedule['name']}'",
        f"mode={schedule['mode']}, targets={', '.join(targets) or 'none'}",
    )
    for name, ok, msg in updates.run_sequence(targets, actor=actor):
        db.log_action(actor, f"result {name}", f"{'ok' if ok else 'failed'}: {msg}")


def coverage(containers: list[dict], schedules: list[dict] | None = None) -> dict[str, dict]:
    """Map each container to the enabled schedules that would update it."""
    schedules = db.list_schedules() if schedules is None else schedules
    active = [s for s in schedules if s["enabled"]]
    result: dict[str, dict] = {}
    for container in containers:
        modes: set[str] = set()
        names: list[str] = []
        if not container["is_self"] and not container["opted_out"]:
            for schedule in active:
                explicit = schedule.get("containers") or []
                included = (
                    container["name"] in explicit
                    or (schedule["mode"] == "selected" and container.get("update_label") is True)
                    or schedule["mode"] == "all"
                )
                if included:
                    names.append(schedule["name"])
                    modes.add("selected" if container["name"] in explicit or schedule["mode"] == "selected" else "all")
        result[container["name"]] = {
            "scheduled": bool(modes),
            "modes": sorted(modes),
            "schedules": names,
        }
    return result


def add_job(schedule: dict) -> None:
    scheduler.add_job(
        run_schedule,
        trigger=CronTrigger.from_crontab(schedule["cron"], timezone=TIMEZONE),
        args=[schedule["id"]],
        id=_job_id(schedule["id"]),
        replace_existing=True,
    )


def remove_job(schedule_id: int) -> None:
    try:
        scheduler.remove_job(_job_id(schedule_id))
    except Exception:
        pass


def refresh_jobs() -> None:
    for schedule in db.list_schedules():
        remove_job(schedule["id"])
        if schedule["enabled"]:
            add_job(schedule)


def start() -> None:
    refresh_jobs()
    if not scheduler.running:
        scheduler.start()


def stop() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
