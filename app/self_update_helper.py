"""Recreate Dock.IO's own container from outside the app process.

Launched by ``dockerops._delegate_self_update`` in a short-lived container from
the freshly pulled image. It has to live outside the web process: that process
is killed when its container is removed, so it cannot finish the recreate.
"""

from __future__ import annotations

import json
import os
import time

import docker

from . import db, dockerops

client = docker.from_env()
api = client.api


def main() -> None:
    payload = json.loads(os.environ.get("DOCKIO_PAYLOAD", "{}"))
    target = payload.get("target") or ""
    create = payload.get("create") or {}
    if not target or not create:
        raise RuntimeError("self-update helper received an empty payload")

    job_id = payload.get("job_id") or ""
    new_image = payload.get("new_image") or ""
    old_image = payload.get("old_image") or ""
    was_running = bool(payload.get("was_running", True))
    was_paused = bool(payload.get("was_paused", False))

    def progress(phase: str, detail: str = "") -> None:
        if not job_id:
            return
        try:
            db.set_update_job(job_id, phase, detail)
        except Exception as exc:
            print(f"could not record progress: {exc}", flush=True)

    def finish(phase: str, detail: str, ok: bool) -> None:
        if job_id:
            try:
                db.set_update_job(job_id, phase, detail, ok=ok, finished=True)
            except Exception as exc:
                print(f"could not record job status: {exc}", flush=True)

    def rollback(reason: str) -> str:
        rb_ok, rb_msg = dockerops.restore_container(
            target, create, old_image, was_running, was_paused, progress=progress
        )
        detail = f"{reason}; {rb_msg if rb_ok else f'ROLLBACK FAILED: {rb_msg}'}"
        finish("failed", detail, False)
        return detail

    # Let the web request return before the app container is stopped.
    time.sleep(3)

    ok, error = dockerops.recreate_container(create, was_running, was_paused)
    if not ok:
        message = rollback(f"{target}: self-update recreate failed: {error}")
        raise RuntimeError(message)

    baseline = dockerops._restart_count(target, 0)
    ok, detail = dockerops.verify_container(
        target,
        expected_image_id=new_image,
        was_running=was_running,
        was_paused=was_paused,
        progress=progress,
        timeout=payload.get("verify_timeout"),
        interval=payload.get("verify_interval"),
        stable_checks=payload.get("verify_stable_checks"),
        baseline_restarts=baseline,
    )
    if not ok:
        message = rollback(detail)
        raise RuntimeError(message)

    cleanup = dockerops._cleanup_old_image(old_image, target, new_image)
    finished = (
        f"{target}: self-update complete, image "
        f"{dockerops._short_id(new_image)}; {detail}"
        + (f"; {cleanup}" if cleanup else "")
    )
    finish("done", finished, True)
    print(finished, flush=True)


if __name__ == "__main__":
    main()
