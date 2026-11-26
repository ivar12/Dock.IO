"""Tracked container updates with phases shown in the UI."""

from __future__ import annotations

import os
import secrets
import threading

from . import db, dockerops, image_updates, timefmt

PHASE_LABELS = {
    "queued": "Queued",
    "inspecting": "Inspecting container",
    "pulling": "Pulling image",
    "recreating": "Recreating container",
    "starting": "Starting container",
    "verifying": "Checking container",
    "cleanup": "Cleaning up old image",
    "done": "Completed",
    "failed": "Failed",
}
PHASES = list(PHASE_LABELS)
WORK_PHASES = [
    "inspecting", "pulling", "recreating", "starting", "verifying", "cleanup"
]


def _history_hours() -> int:
    try:
        return max(1, int(os.getenv("UPDATE_HISTORY_HOURS", "24")))
    except (TypeError, ValueError):
        return 24


JOB_HISTORY_HOURS = _history_hours()


def _new_job_id() -> str:
    return secrets.token_hex(8)


def _execute(job_id: str, name: str, actor: str) -> tuple[str, bool, str]:
    def progress(phase: str, detail: str = "") -> None:
        db.set_update_job(job_id, phase, detail)

    is_self = name == dockerops.SELF_NAME
    delegated = False
    try:
        if is_self:
            ok, msg, delegated = dockerops.update_container(
                name, progress=progress, job_id=job_id
            )
        else:
            ok, msg, delegated = dockerops.update_container(name, progress=progress)
    except Exception as exc:
        ok, msg, delegated = False, str(exc), False

    # A delegated self-update is finished by the helper in the restarted app.
    # Every other update (including a self-update with an unchanged image) is
    # finished here, after the container check inside update_container().
    if not delegated:
        db.set_update_job(job_id, "done" if ok else "failed", msg, ok=ok, finished=True)
        if ok:
            image_updates.refresh(force=True)
    db.log_action(actor, "update", f"{name}: {'ok' if ok else 'failed'} - {msg}")
    return name, ok, msg


def start_update(name: str, actor: str) -> str:
    """Start one tracked update in the background and return its job id."""
    job_id = _new_job_id()
    db.create_update_job(job_id, name, actor)
    threading.Thread(
        target=_execute, args=(job_id, name, actor), daemon=True, name=f"update-{name}"
    ).start()
    return job_id


def run_sequence(names: list[str], actor: str) -> list[tuple[str, bool, str]]:
    """Run several tracked updates one at a time (used by schedules)."""
    results = []
    for name in names:
        job_id = _new_job_id()
        db.create_update_job(job_id, name, actor)
        results.append(_execute(job_id, name, actor))
    return results


def status() -> dict:
    """Snapshot of running and recently finished updates."""
    db.delete_old_update_jobs(hours=JOB_HISTORY_HOURS)
    jobs = db.list_update_jobs(limit=50)
    active = []
    for job in jobs:
        finished = bool(job.get("finished_at"))
        job["started_display"] = timefmt.format_time(job.get("started_at"))
        job["finished_display"] = timefmt.format_time(job.get("finished_at"))
        phase = job["phase"]
        job["label"] = PHASE_LABELS.get(phase, phase)
        job["active"] = not finished
        if finished:
            job["step"] = len(WORK_PHASES)
            job["percent"] = 100
        elif phase in WORK_PHASES:
            job["step"] = WORK_PHASES.index(phase) + 1
            job["percent"] = round(100 * job["step"] / len(WORK_PHASES))
        else:
            job["step"] = 0
            job["percent"] = 0
        if not finished:
            active.append(job)
    return {
        "jobs": jobs,
        "active": active,
        "total_steps": len(WORK_PHASES),
        "history_hours": JOB_HISTORY_HOURS,
    }


def reconcile_after_restart() -> None:
    """Close self-update jobs left open by a restart after checking the app."""
    for job in db.active_update_jobs():
        if job["container"] != dockerops.SELF_NAME:
            continue
        job_id = job["id"]

        def progress(phase: str, detail: str = "") -> None:
            db.set_update_job(job_id, phase, detail)

        ok, detail = dockerops.verify_container(
            dockerops.SELF_NAME,
            was_running=True,
            progress=progress,
        )
        db.set_update_job(
            job_id,
            "done" if ok else "failed",
            f"{detail} (checked after restart)",
            ok=ok,
            finished=True,
        )
