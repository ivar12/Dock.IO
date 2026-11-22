"""Cached registry digest checks so container lists can show image update status."""

from __future__ import annotations

import re
import threading
import time

import httpx

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


VERSION_TAG = re.compile(r"^v?(\d+)\.(\d+)(?:\.(\d+))?(?:[-_.+].*)?$")
TAGS_TTL = 600.0
_tag_lock = threading.Lock()
_tag_cache: dict[str, tuple[float, list[str]]] = {}


def _result(state: str, label: str, css: str, detail: str = "") -> dict:
    return {"state": state, "label": label, "css": css, "detail": detail}


def _image_tag(image: str) -> str:
    if "@" in image:
        return ""
    last = image.rsplit("/", 1)[-1]
    return last.rsplit(":", 1)[-1] if ":" in last else "latest"


def _parse_version(tag: str) -> tuple[tuple[int, ...], str] | None:
    match = VERSION_TAG.match(str(tag or ""))
    if not match:
        return None
    major, minor, patch = match.groups()
    version = (int(major), int(minor), int(patch or 0))
    display = tag[1:] if tag.startswith("v") else tag
    return version, display


def _format_version(version: tuple[int, ...]) -> str:
    return ".".join(str(part) for part in version)


def _registry_tags(image: str) -> list[str] | None:
    """List registry tags for an image, or None when the registry cannot be reached."""
    repo, _ = dockerops._split_image(image)
    repo = repo.split("@", 1)[0]
    first, sep, rest = repo.partition("/")
    if not sep or ("." not in first and ":" not in first and first != "localhost"):
        host, name = "registry-1.docker.io", repo
        if "/" not in name:
            name = f"library/{name}"
    else:
        host, name = first, rest

    with _tag_lock:
        cached = _tag_cache.get(repo)
        if cached and time.time() - cached[0] < TAGS_TTL:
            return cached[1]

    url = f"https://{host}/v2/{name}/tags/list"
    try:
        with httpx.Client(timeout=5.0, follow_redirects=True) as client:
            response = client.get(url)
            if response.status_code == 401:
                challenge = client.get(f"https://{host}/v2/").headers.get("www-authenticate", "")
                params = dict(re.findall(r'(\w+)="([^"]+)"', challenge))
                realm = params.get("realm")
                if not realm:
                    return None
                token_response = client.get(
                    realm,
                    params={
                        "service": params.get("service", host),
                        "scope": f"repository:{name}:pull",
                    },
                )
                token_response.raise_for_status()
                data = token_response.json()
                token = data.get("token") or data.get("access_token")
                if not token:
                    return None
                response = client.get(url, headers={"Authorization": f"Bearer {token}"})
            response.raise_for_status()
            tags = response.json().get("tags") or []
    except Exception:
        return None

    with _tag_lock:
        _tag_cache[repo] = (time.time(), tags)
    return tags


def _newer_in_train(tag: str, tags: list[str]) -> tuple[int, ...] | None:
    parsed = _parse_version(tag)
    if not parsed:
        return None
    current, _ = parsed
    best = None
    for candidate in tags:
        found = _parse_version(candidate)
        if not found:
            continue
        version, _ = found
        if version[:2] != current[:2] or version <= current:
            continue
        if best is None or version > best:
            best = version
    return best


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

        tag = _image_tag(image)
        parsed = _parse_version(tag)
        if parsed:
            current, display = parsed
            tags = _registry_tags(image)
            if tags is None:
                result = _result(
                    result["state"],
                    f"{display} · {result['label'].lower()}",
                    result["css"],
                    result["detail"],
                )
            else:
                newer = _newer_in_train(tag, tags)
                if newer:
                    result = _result(
                        "update",
                        f"{_format_version(newer)} · available",
                        "update",
                        f"pinned {display}; newer in {current[0]}.{current[1]} train: {_format_version(newer)}",
                    )
                else:
                    result = _result(
                        "up-to-date",
                        f"{display} · up to date",
                        "yes",
                        f"pinned {display}; no newer release in {current[0]}.{current[1]} train",
                    )

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
