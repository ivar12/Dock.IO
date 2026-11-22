"""Running version and cached GitHub release check."""

from __future__ import annotations

import re
import threading
import time

import httpx

VERSION = "1.2.0"
RELEASES_API = "https://api.github.com/repos/ivar12/Dock.IO/releases/latest"
CACHE_TTL = 3600.0

_lock = threading.Lock()
_state = {
    "checked_at": 0.0,
    "latest": "",
    "url": "",
    "error": "",
    "busy": False,
}


def _version_tuple(tag: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", tag or "")[:3]
    return tuple(int(p) for p in parts) or (0,)


def _fetch() -> None:
    try:
        with httpx.Client(timeout=5.0, follow_redirects=True) as client:
            response = client.get(
                RELEASES_API,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "dockio-version-check",
                },
            )
            response.raise_for_status()
            data = response.json()
        _state["latest"] = str(data.get("tag_name") or "").lstrip("v")
        _state["url"] = str(data.get("html_url") or "")
        _state["error"] = ""
    except Exception as exc:
        _state["error"] = str(exc)[:200]
    finally:
        _state["checked_at"] = time.time()
        _state["busy"] = False


def refresh(force: bool = False) -> None:
    """Start a background refresh when the cache is stale (single flight)."""
    with _lock:
        if _state["busy"]:
            return
        if not force and time.time() - _state["checked_at"] < CACHE_TTL:
            return
        _state["busy"] = True
    threading.Thread(target=_fetch, daemon=True, name="dockio-version-check").start()


def check_now() -> dict:
    """Run a synchronous check (used by the Check button)."""
    with _lock:
        if _state["busy"]:
            return update_info()
        _state["busy"] = True
    _fetch()
    return update_info()


def update_info() -> dict:
    refresh()
    latest = _state["latest"]
    return {
        "current": VERSION,
        "latest": latest,
        "url": _state["url"],
        "available": bool(latest) and _version_tuple(latest) > _version_tuple(VERSION),
        "error": _state["error"],
        "checked_at": _state["checked_at"],
    }
