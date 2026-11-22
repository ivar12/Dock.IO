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

    def finish(phase: str, detail: str, ok: bool) -> None:
        if job_id:
            try:
                db.set_update_job(job_id, phase, detail, ok=ok, finished=True)
            except Exception as exc:
                print(f"could not record job status: {exc}", flush=True)

    # Let the web request return before the app container is stopped.
    time.sleep(3)

    try:
        api.remove_container(target, force=True)
    except docker.errors.NotFound:
        pass

    error = ""
    for attempt in range(3):
        try:
            created = api.create_container(**create)
            if payload.get("was_running"):
                api.start(created["Id"])
            break
        except Exception as exc:
            error = str(exc)
            time.sleep(1 + attempt)
    else:
        message = f"{target}: self-update recreate failed: {error}"
        finish("failed", message, False)
        raise RuntimeError(message)

    current = (api.inspect_container(target) or {}).get("Image", "")
    if new_image and current != new_image:
        message = (
            f"{target}: container uses {dockerops._short_id(current)} "
            f"instead of pulled image {dockerops._short_id(new_image)}"
        )
        finish("failed", message, False)
        raise RuntimeError(message)

    cleanup = dockerops._cleanup_old_image(payload.get("old_image") or "", target, new_image)
    detail = (
        f"{target}: self-update complete, image "
        f"{dockerops._short_id(current or new_image)}"
        + (f"; {cleanup}" if cleanup else "")
    )
    finish("done", detail, True)
    print(detail, flush=True)


if __name__ == "__main__":
    main()
