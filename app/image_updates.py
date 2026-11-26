"""Cached registry digest checks so container lists can show image update status."""

from __future__ import annotations

import re
import threading
import time
from urllib.parse import quote

import httpx

from . import dockerops

TTL_SECONDS = 600.0
HUB_API = "https://hub.docker.com/v2/repositories"
TAG_PAGE_SIZE = 100
MAX_TAG_PAGES = 5
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


class _Unavailable(Exception):
    """Registry metadata could not be checked without spending a pull."""


def _docker_hub_repo(repo: str) -> tuple[str, str] | None:
    """Return (namespace, name) for a Docker Hub repository, else None."""
    if not repo:
        return None
    first, sep, rest = repo.partition("/")
    if first in ("docker.io", "index.docker.io", "registry-1.docker.io"):
        namespace, slash, name = rest.partition("/")
        if not namespace:
            return None
        return (namespace, name) if slash else ("library", namespace)
    if not sep or ("." not in first and ":" not in first and first != "localhost"):
        return (first, rest) if sep else ("library", first)
    return None


def _hub_get(path: str, params: dict[str, str | int] | None = None) -> dict | None:
    """Read Docker Hub metadata; Hub API limits are separate from pull limits."""
    try:
        with httpx.Client(timeout=5.0, follow_redirects=True) as client:
            response = client.get(f"{HUB_API}{path}", params=params)
            if response.status_code == 429:
                return None
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, dict) else None
    except Exception:
        return None


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


VERSION_LABELS = (
    "org.opencontainers.image.version",
    "version",
)
GENERIC_TAGS = {"latest", "main", "master", "stable", "dev", "edge"}


def image_version(
    image: str,
    labels: dict | None = None,
    digests: list[str] | str | None = None,
    image_id: str = "",
) -> str:
    """Return the most exact version available for an image reference.

    An image's embedded version label wins over its tag, so ``:latest`` can
    show the real version. If no version is available, a non-version tag falls
    back to the image digest, which identifies the exact image contents.
    """
    text = str(image or "").strip()
    if not text:
        return "-"

    for key in VERSION_LABELS:
        value = str((labels or {}).get(key) or "").strip()
        if value and value.lower() not in GENERIC_TAGS:
            return value

    if "@" in text:
        return f"digest {_short(_digest(text.rsplit('@', 1)[-1]))}"

    tag = _image_tag(text)
    parsed = _parse_version(tag)
    if parsed:
        return parsed[1]

    candidates = [digests] if isinstance(digests, str) else list(digests or [])
    if image_id:
        candidates.append(image_id)
    for candidate in candidates:
        value = str(candidate or "").strip()
        if value:
            return f"digest {_short(_digest(value))}"
    return tag or "-"


def _hub_tag_digest(repo: str, tag: str) -> str | None:
    """Get a tag's digest from Docker Hub metadata without a registry pull."""
    hub = _docker_hub_repo(repo)
    if not hub or not tag:
        return None

    namespace, name = hub
    path = (
        f"/{quote(namespace, safe='')}/{quote(name, safe='')}"
        f"/tags/{quote(tag, safe='')}"
    )
    data = _hub_get(path)
    digest = _digest(data.get("digest")) if data else ""
    return digest or None


def _hub_tags(repo: str, name_filter: str) -> list[str] | None:
    """List matching Docker Hub tags from the metadata API, not the registry."""
    hub = _docker_hub_repo(repo)
    if not hub:
        return None

    key = f"tags:{repo}:{name_filter}"
    with _tag_lock:
        cached = _tag_cache.get(key)
        if cached and time.time() - cached[0] < TAGS_TTL:
            return cached[1]

    namespace, name = hub
    path = f"/{quote(namespace, safe='')}/{quote(name, safe='')}/tags"
    tags: list[str] = []
    for page in range(1, MAX_TAG_PAGES + 1):
        data = _hub_get(path, {
            "name": name_filter,
            "page_size": TAG_PAGE_SIZE,
            "page": page,
        })
        if data is None:
            return None
        for item in data.get("results") or []:
            tag = item.get("name") if isinstance(item, dict) else None
            if tag:
                tags.append(tag)
        if not data.get("next"):
            break

    with _tag_lock:
        _tag_cache[key] = (time.time(), tags)
    return tags


def _registry_tags(image: str, name_filter: str = "") -> list[str] | None:
    """List tags for an image, or None when the registry cannot be reached."""
    repo, _ = dockerops._split_image(image)
    repo = repo.split("@", 1)[0]
    if _docker_hub_repo(repo):
        return _hub_tags(repo, name_filter)

    first, sep, rest = repo.partition("/")
    if not sep or ("." not in first and ":" not in first and first != "localhost"):
        host, name = "registry-1.docker.io", repo
        if "/" not in name:
            name = f"library/{name}"
    else:
        host, name = first, rest

    key = f"registry:{repo}:{name_filter}"
    with _tag_lock:
        cached = _tag_cache.get(key)
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
        _tag_cache[key] = (time.time(), tags)
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


def _remote_digest(image: str, tag: str) -> str:
    """Return the registry digest without spending a Docker Hub pull."""
    if "@" in image:
        return _digest(image.rsplit("@", 1)[-1])

    repo, _ = dockerops._split_image(image)
    repo = repo.split("@", 1)[0]
    if _docker_hub_repo(repo):
        digest = _hub_tag_digest(repo, tag)
        if digest is None:
            raise _Unavailable("Docker Hub metadata unavailable")
        return digest
    return _digest(dockerops.client.images.get_registry_data(image).id)


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
        tag = _image_tag(image)
        try:
            remote = _remote_digest(image, tag)
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
        except _Unavailable as exc:
            result = _result("unknown", "Cannot check", "", str(exc))
        except Exception as exc:
            message = str(exc).lower()
            if any(word in message for word in ("unauthorized", "denied", "not found", "403", "401", "404")):
                result = _result("unknown", "Cannot check", "", "Registry check unavailable for this image")
            else:
                result = _result("error", "Check failed", "no", str(exc)[:200])

        parsed = _parse_version(tag)
        if parsed:
            current, display = parsed
            tags = _registry_tags(image, f"{current[0]}.{current[1]}")
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
