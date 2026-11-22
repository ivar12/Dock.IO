"""Tracked container updates with phases shown in the UI."""

from __future__ import annotations

import secrets
import threading

from . import db, dockerops

PHASE_LABELS = {
    "queued": "Queued",
    "inspecting": "Inspecting container",
    "pulling": "Pulling image",
    "recreating": "Recreating container",
    "starting": "Starting container",
    "cleanup": "Cleaning up old image",
    "done": "Completed",
    "failed": "Failed",
}
PHASES = list(PHASE_LABELS)
WORK_PHASES = ["inspecting", "pulling", "recreating", "starting", "cleanup"]


def _new_job_id() -> str:
    return secrets.token_hex(8)


def _execute(job_id: str, name: str, actor: str) -> tuple[str, bool, str]:
    def progress(phase: str, detail: str = "") -> None:
        db.set_update_job(job_id, phase, detail)

    is_self = name == dockerops.SELF_NAME
    try:
        if is_self:
            ok, msg = dockerops.update_container(name, progress=progress, job_id=job_id)
        else:
            ok, msg = dockerops.update_container(name, progress=progress)
    except Exception as exc:
        ok, msg = False, str(exc)

    # A successful self-update is finished by the helper in the restarted app.
    if not (is_self and ok):
        db.set_update_job(job_id, "done" if ok else "failed", msg, ok=ok, finished=True)
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
    """Snapshot of currently running updates (finished jobs leave the list)."""
    db.delete_old_update_jobs()
    jobs = db.active_update_jobs()
    for job in jobs:
        phase = job["phase"]
        job["label"] = PHASE_LABELS.get(phase, phase)
        job["active"] = True
        if phase in WORK_PHASES:
            job["step"] = WORK_PHASES.index(phase) + 1
            job["percent"] = round(100 * job["step"] / len(WORK_PHASES))
        else:
            job["step"] = 0
            job["percent"] = 0
    return {
        "jobs": jobs,
        "active": jobs,
        "total_steps": len(WORK_PHASES),
    }
