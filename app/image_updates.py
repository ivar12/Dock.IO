"""Cached registry digest checks so container lists can show image update status."""

from __future__ import annotations

import threading
import time

from . import dockerops

TTL_SECONDS = 600.0
_lock = threading.Lock()
_state = {
    "checked_at": 0.0,
    "busy": False,
    "images": {},
}


def _short(digest: str) -> str:
    value = str(digest or "")
    return value.replace("sha256:", "")[:12]


def _digest(value: str) -> str:
    """Normalise a digest from either 'sha256:...' or 'repo@sha256:...'."""
    text = str(value or "")
    return text.rsplit("@", 1)[-1] if "@" in text else text


def _result(state: str, label: str, css: str, detail: str = "") -> dict:
    return {"state": state, "label": label, "css": css, "detail": detail}


CHECKING = _result("checking", "Checking...", "", "Registry check in progress")
UNKNOWN = _result("unknown", "Unknown", "", "No local image digest to compare")


def _aliases(image: str) -> list[str]:
    names = [image]
    last = image.rsplit("/", 1)[-1]
    if last == "latest" or ":" not in last:
        names.append(image.rsplit(":", 1)[0] if ":" in last else image + ":latest")
    return names


def _check_all() -> None:
    results: dict[str, dict] = {}
    try:
        containers = dockerops.list_containers()
    except Exception as exc:
        results["__error__"] = _result("error", "Check failed", "no", str(exc)[:200])
        containers = []

    targets: dict[str, dict] = {}
    for container in containers:
        image = container.get("configured_image") or container.get("image") or ""
        if not image or image in targets:
            continue
        targets[image] = {
            "container": container["name"],
            "local": container.get("repo_digests") or [],
        }

    for image, info in targets.items():
        try:
            remote = _digest(dockerops.client.images.get_registry_data(image).id)
            local = {_digest(value) for value in info["local"]}
            if not local:
                result = _result("unknown", "Unknown", "", "No local image digest to compare")
            elif remote in local:
                result = _result(
                    "up-to-date", "Up to date", "yes", f"digest {_short(remote)}"
                )
            else:
                result = _result(
                    "update",
                    "available",
                    "update",
                    f"local {_short(next(iter(local)))} / registry {_short(remote)}",
                )
        except Exception as exc:
            message = str(exc).lower()
            if any(word in message for word in ("unauthorized", "denied", "not found", "403", "401", "404")):
                result = _result("unknown", "Cannot check", "", "Registry check unavailable for this image")
            else:
                result = _result("error", "Check failed", "no", str(exc)[:200])

        for alias in _aliases(image):
            results[alias] = result

    with _lock:
        _state["images"] = results
        _state["checked_at"] = time.time()
        _state["busy"] = False


def refresh(force: bool = False) -> None:
    """Start one background registry check when the cache is stale."""
    with _lock:
        if _state["busy"]:
            return
        if not force and time.time() - _state["checked_at"] < TTL_SECONDS:
            return
        _state["busy"] = True
    threading.Thread(target=_check_all, daemon=True, name="dockio-image-updates").start()


def busy() -> bool:
    return bool(_state["busy"])


def checked_at() -> float:
    return float(_state["checked_at"])


def state_for(container: dict) -> dict:
    refresh()
    image = container.get("configured_image") or container.get("image") or ""
    with _lock:
        result = _state["images"].get(image)
        if result is None and container.get("image"):
            result = _state["images"].get(container["image"])
        is_busy = bool(_state["busy"])
    if result is not None:
        return result
    return CHECKING if is_busy else UNKNOWN


def summary(containers: list[dict]) -> dict:
    states = {c["name"]: state_for(c) for c in containers}
    return {
        "busy": busy(),
        "checked_at": checked_at(),
        "states": states,
        "available": sum(s["state"] == "update" for s in states.values()),
    }
