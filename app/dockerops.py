"""Docker operations: inventory, start/stop/restart and in-place image updates."""

from __future__ import annotations

import inspect
import json
import os
import re
import secrets
import time

import docker

from . import timefmt

client = docker.from_env()
api = client.api

SELF_NAME = os.getenv("SELF_CONTAINER", "dockio")


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        return min(maximum, max(minimum, float(os.getenv(name, str(default)))))
    except (TypeError, ValueError):
        return default


# A container is only reported as updated once it stays in a good state.
VERIFY_TIMEOUT = _env_float("UPDATE_VERIFY_TIMEOUT", 60.0, 1.0, 900.0)
VERIFY_INTERVAL = _env_float("UPDATE_VERIFY_INTERVAL", 2.0, 0.25, 10.0)
VERIFY_STABLE_CHECKS = _env_int("UPDATE_VERIFY_STABLE_CHECKS", 2)
CREATE_ATTEMPTS = _env_int("UPDATE_CREATE_ATTEMPTS", 3)
OPT_OUT_LABELS = (
    "dockio.update",
    "com.centurylinklabs.watchtower.enable",
)


def _bool_label(labels: dict, key: str) -> bool | None:
    value = labels.get(key)
    if value is None:
        return None
    return str(value).lower() in ("1", "true", "yes", "on")


def _valid_label_values() -> tuple[str, ...]:
    return ("1", "true", "yes", "on", "0", "false", "no", "off")


def list_containers() -> list[dict]:
    result = []
    for c in client.containers.list(all=True):
        labels = c.labels or {}
        attrs = c.attrs or {}
        state = attrs.get("State") or {}
        configured_image = (attrs.get("Config") or {}).get("Image", "")

        image = ""
        repo_digests = []
        image_labels = {}
        try:
            if c.image:
                image_attrs = c.image.attrs or {}
                repo_digests = image_attrs.get("RepoDigests") or []
                image_labels = (image_attrs.get("Config") or {}).get("Labels") or {}
                if c.image.tags:
                    image = c.image.tags[0]
        except Exception:
            pass
        if not image:
            image = configured_image

        raw_update_label = labels.get("dockio.update")
        invalid_label = (
            raw_update_label is not None
            and str(raw_update_label).lower() not in _valid_label_values()
        )

        result.append(
            {
                "id": c.id[:12],
                "name": c.name,
                "image": image,
                "configured_image": configured_image,
                "repo_digests": repo_digests,
                "image_labels": image_labels,
                "image_missing": not bool(image),
                "status": c.status,
                "running": bool(state.get("Running")),
                "health": (state.get("Health") or {}).get("Status"),
                "labels": labels,
                "update_label": _bool_label(labels, "dockio.update"),
                "invalid_update_label": invalid_label,
                "opted_out": any(_bool_label(labels, k) is False for k in OPT_OUT_LABELS),
                "is_self": c.name == SELF_NAME,
                "ports": (attrs.get("NetworkSettings") or {}).get("Ports", {}) or {},
            }
        )
    return sorted(result, key=lambda x: x["name"])


def get_container(name: str):
    return client.containers.get(name)


def action(name: str, act: str) -> str:
    container = get_container(name)
    if act == "start":
        container.start()
    elif act == "stop":
        container.stop()
    elif act == "restart":
        container.restart()
    else:
        raise ValueError(f"unknown action: {act}")
    return f"{name}: {act} ok"


def _split_image(image: str) -> tuple[str, str | None]:
    """Split 'registry:5000/repo:tag' into repository and tag."""
    if "@" in image:
        return image, None
    last = image.rsplit("/", 1)[-1]
    if ":" in last:
        repo, tag = image.rsplit(":", 1)
        return repo, tag
    return image, "latest"


def _report(progress, phase: str, detail: str = "") -> None:
    if progress:
        progress(phase, detail)


def _short_id(image_id: str) -> str:
    return str(image_id or "").replace("sha256:", "")[:12]


def _image_users(image_id: str) -> list[str]:
    """Containers actually created from this exact image (not its descendants)."""
    users = []
    for container in client.containers.list(all=True):
        if ((container.attrs or {}).get("Image") or "") == image_id:
            users.append(container.name)
    return sorted(users)


def _docker_socket_bind(info: dict) -> str:
    """Find the Docker socket bind so a helper container can reach the API."""
    host = info.get("HostConfig") or {}
    for bind in host.get("Binds") or []:
        if str(bind).split(":", 1)[0] == "/var/run/docker.sock":
            return str(bind)
    for mount in info.get("Mounts") or []:
        if mount.get("Destination") == "/var/run/docker.sock" and mount.get("Type") == "bind":
            return f"{mount.get('Source')}:/var/run/docker.sock"
    return ""


def _data_mounts(info: dict) -> list[dict]:
    """Mounts pointing at the app's data directory (shared with the helper)."""
    data_dir = os.getenv("DATA_DIR", "/app/data")
    mounts = []
    for mount in info.get("Mounts") or []:
        if mount.get("Destination") != data_dir:
            continue
        source = mount.get("Name") if mount.get("Type") == "volume" else mount.get("Source")
        item = {
            "Type": mount.get("Type", "volume"),
            "Source": source or mount.get("Source"),
            "Target": mount.get("Destination"),
            "RW": bool(mount.get("RW", True)),
        }
        for key in ("BindOptions", "VolumeOptions", "TmpfsOptions"):
            if mount.get(key):
                item[key] = mount[key]
        mounts.append(item)
    return mounts


def _delegate_self_update(
    info: dict,
    name: str,
    image: str,
    old_image_id: str,
    new_image_id: str,
    create_kwargs: dict,
    was_running: bool,
    was_paused: bool = False,
    job_id: str = "",
    progress=None,
) -> tuple[bool, str]:
    """Hand the recreate step to a short-lived helper container.

    Removing this app's own container kills the thread doing the update, so a
    helper outside the app process performs the stop/create/start sequence.
    """
    socket_bind = _docker_socket_bind(info)
    if not socket_bind:
        return False, (
            f"{name}: cannot update itself - no /var/run/docker.sock bind found; "
            "mount the Docker socket in the Compose file"
        )

    helper_name = f"dockio-self-update-{secrets.token_hex(4)}"
    payload = {
        "target": name,
        "create": create_kwargs,
        "new_image": new_image_id,
        "old_image": old_image_id,
        "was_running": bool(was_running),
        "was_paused": bool(was_paused),
        "verify_timeout": VERIFY_TIMEOUT,
        "verify_interval": VERIFY_INTERVAL,
        "verify_stable_checks": VERIFY_STABLE_CHECKS,
        "job_id": job_id,
    }
    helper_host = {
        "Binds": [socket_bind],
        "Mounts": _data_mounts(info),
        "AutoRemove": True,
        "RestartPolicy": {"Name": "no"},
        "NetworkMode": "default",
    }
    try:
        created = api.create_container(
            name=helper_name,
            image=new_image_id or image,
            command=["python", "-m", "app.self_update_helper"],
            environment=[f"DOCKIO_PAYLOAD={json.dumps(payload)}"],
            host_config=helper_host,
        )
        api.start(created["Id"])
    except Exception as exc:
        return False, f"{name}: could not start self-update helper: {exc}"

    _report(progress, "recreating", f"Handed to {helper_name}; Dock.IO will restart shortly")
    return True, (
        f"{name}: self-update started via {helper_name} "
        f"(image {_short_id(new_image_id)}); the app restarts in a few seconds"
    )


def _pull(image: str, progress=None) -> tuple[bool, str]:
    repo, tag = _split_image(image)
    last_line = ""
    _report(progress, "pulling", f"Pulling {image}")
    try:
        if tag is None:
            stream = api.pull(repo, stream=True, decode=True)
        else:
            stream = api.pull(repo, tag=tag, stream=True, decode=True)
        for line in stream:
            if isinstance(line, dict) and line.get("status"):
                last_line = str(line["status"])
            elif isinstance(line, str):
                last_line = line
            if last_line:
                _report(progress, "pulling", last_line)
        return True, last_line or "image pulled"
    except Exception as exc:  # docker.errors.APIError, image not found, registry down, ...
        return False, f"pull failed: {exc}"


def _supported_kwargs(func, kwargs: dict) -> dict:
    params = inspect.signature(func).parameters
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return kwargs
    return {k: v for k, v in kwargs.items() if k in params}


def _cleanup_old_image(old_image_id: str, container_name: str, new_image_id: str = "") -> str:
    """Remove the superseded image once the container is known to use the new one."""
    if not old_image_id:
        return ""
    try:
        current = (api.inspect_container(container_name) or {}).get("Image", "")
    except Exception as exc:
        return f"could not verify new image: {exc}"
    if new_image_id and current != new_image_id:
        return f"old image kept: container uses {_short_id(current)}, not pulled {_short_id(new_image_id)}"
    if current == old_image_id:
        return "old image unchanged (no newer image was pulled)"

    users = _image_users(old_image_id)
    if users:
        return f"old image kept (still in use by: {', '.join(users)})"

    # force=True also drops stale tags/layer references left by an earlier pull.
    for force in (False, True):
        try:
            api.remove_image(old_image_id, force=force)
            return f"old image {_short_id(old_image_id)} removed"
        except Exception as exc:
            if force:
                return f"old image cleanup skipped: {exc}"
    return ""


def _remove_if_exists(name: str) -> tuple[bool, str]:
    """Remove a container by name, treating an already-absent container as ok."""
    try:
        api.remove_container(name, force=True)
    except docker.errors.NotFound:
        pass
    except Exception as exc:
        return False, str(exc)
    return True, ""


def _create_and_start(
    create_kwargs: dict, was_running: bool, was_paused: bool = False
) -> tuple[bool, str]:
    """Create (and optionally start) the container, retrying transient conflicts."""
    name = create_kwargs.get("name", "")
    last_error = ""
    for attempt in range(CREATE_ATTEMPTS):
        try:
            created = api.create_container(**create_kwargs)
            container_id = created["Id"]
            if was_running:
                api.start(container_id)
            if was_paused:
                api.pause(container_id)
            return True, ""
        except Exception as exc:
            last_error = str(exc)
            # A failed start can leave a created container behind; clear it so the
            # next attempt (and any rollback) can reuse the original name.
            _remove_if_exists(name)
            if attempt + 1 < CREATE_ATTEMPTS:
                time.sleep(1 + attempt)
    return False, last_error


def recreate_container(
    create_kwargs: dict, was_running: bool, was_paused: bool = False
) -> tuple[bool, str]:
    """Replace the named container using the supplied create configuration."""
    name = create_kwargs.get("name", "")
    ok, error = _remove_if_exists(name)
    if not ok:
        return False, f"{name}: could not remove old container: {error}"
    return _create_and_start(create_kwargs, was_running, was_paused)


def _restart_count(name: str, default: int | None = 0) -> int | None:
    try:
        state = (api.inspect_container(name) or {}).get("State") or {}
        return int(state.get("RestartCount") or 0)
    except Exception:
        return default


def _state_ok(
    info: dict,
    was_running: bool,
    was_paused: bool,
    expected_image_id: str,
    baseline_restarts: int | None,
) -> tuple[bool, str]:
    state = info.get("State") or {}
    image_id = info.get("Image") or ""
    restarts = int(state.get("RestartCount") or 0)

    if expected_image_id and image_id != expected_image_id:
        return False, f"uses image {_short_id(image_id)}, expected {_short_id(expected_image_id)}"
    if state.get("Dead"):
        return False, "container is dead"
    if state.get("Restarting") or state.get("Status") == "restarting":
        return False, "container is restarting"
    if baseline_restarts is not None and restarts > baseline_restarts:
        return False, f"restarted {restarts - baseline_restarts} time(s) during the check"

    # Health only matters for a running, unpaused container; a stopped or
    # paused container cannot report a fresh healthy result.
    health = (state.get("Health") or {}).get("Status")
    if was_running and not was_paused and health and health != "healthy":
        return False, f"health check is {health}"

    if was_running:
        if not state.get("Running"):
            return False, f"container is {state.get('Status') or 'not running'}"
        if bool(state.get("Paused")) != bool(was_paused):
            return False, "container is unexpectedly paused" if state.get("Paused") else "container is not paused"
    elif state.get("Running"):
        return False, "container started even though it was stopped before the update"
    return True, "ok"


def _state_description(info: dict, was_running: bool, was_paused: bool) -> str:
    state = info.get("State") or {}
    health = (state.get("Health") or {}).get("Status")
    parts = ["running" if was_running else "stopped"]
    if was_paused:
        parts.append("paused")
    if health:
        parts.append(health)
    return ", ".join(parts)


def verify_container(
    name: str,
    expected_image_id: str = "",
    was_running: bool = True,
    was_paused: bool = False,
    progress=None,
    timeout: float | None = None,
    interval: float | None = None,
    stable_checks: int | None = None,
    baseline_restarts: int | None = None,
) -> tuple[bool, str]:
    """Wait until the container is running and healthy, not merely started."""
    timeout = VERIFY_TIMEOUT if timeout is None else max(1.0, float(timeout))
    interval = VERIFY_INTERVAL if interval is None else min(10.0, max(0.25, float(interval)))
    stable_checks = VERIFY_STABLE_CHECKS if stable_checks is None else max(1, int(stable_checks))
    if baseline_restarts is None:
        baseline_restarts = _restart_count(name, default=None)

    deadline = time.monotonic() + timeout
    stable = 0
    last = "not checked yet"
    info: dict = {}
    while True:
        try:
            info = api.inspect_container(name) or {}
            ok, last = _state_ok(
                info, was_running, was_paused, expected_image_id, baseline_restarts
            )
        except Exception as exc:
            ok, last = False, f"inspect failed: {exc}"
        stable = stable + 1 if ok else 0
        if stable >= stable_checks:
            detail = _state_description(info, was_running, was_paused)
            return True, f"{name}: verified {detail}, image {_short_id(info.get('Image'))}"
        if time.monotonic() >= deadline:
            break
        _report(progress, "verifying", f"Checking {name}: {last}")
        time.sleep(interval)
    return False, f"{name}: not ready after {timeout:g}s ({last})"


def restore_container(
    name: str,
    create_kwargs: dict,
    old_image_id: str,
    was_running: bool,
    was_paused: bool = False,
    progress=None,
) -> tuple[bool, str]:
    """Put the previous image/container back after a failed update."""
    if not old_image_id:
        return False, f"{name}: cannot roll back, previous image id is unknown"
    _report(progress, "recreating", f"Rolling {name} back to the previous image")
    restore = dict(create_kwargs)
    restore["image"] = old_image_id
    ok, error = recreate_container(restore, was_running, was_paused)
    if not ok:
        return False, f"{name}: rollback failed: {error}"
    ok, detail = verify_container(
        name,
        expected_image_id=old_image_id,
        was_running=was_running,
        was_paused=was_paused,
        progress=progress,
    )
    if not ok:
        return False, f"{name}: rollback container is not healthy: {detail}"
    return True, f"{name}: restored previous container ({detail})"


def update_container(
    name: str, progress=None, job_id: str = ""
) -> tuple[bool, str, bool]:
    """Pull the configured image and recreate the container with the same config.

    Returns ``(ok, detail, delegated)``. ``delegated`` is True only when Dock.IO
    handed its own recreate to the helper container, which finishes the job.
    """
    _report(progress, "inspecting", f"Reading configuration of {name}")
    try:
        info = api.inspect_container(name)
    except Exception as exc:
        return False, f"{name}: inspect failed: {exc}", False

    old_image_id = info.get("Image") or ""
    state = info.get("State") or {}
    was_running = bool(state.get("Running"))
    was_paused = bool(state.get("Paused"))

    cfg = info.get("Config") or {}
    host = info.get("HostConfig") or {}
    networks = info.get("NetworkSettings", {}).get("Networks", {}) or {}
    image = cfg.get("Image") or ""
    if not image:
        return False, f"{name}: container has no image", False

    ok, pull_msg = _pull(image, progress=progress)
    if not ok:
        return False, f"{name}: {pull_msg}", False

    try:
        new_image_id = (api.inspect_image(image) or {}).get("Id", "")
    except Exception as exc:
        return False, f"{name}: cannot inspect pulled image: {exc}", False
    if new_image_id:
        _report(progress, "pulling", f"Using image {_short_id(new_image_id)}")

    if new_image_id and old_image_id and new_image_id == old_image_id:
        # No new image: do not recreate unnecessarily, but still confirm the
        # existing container is in a good state before calling this complete.
        _report(progress, "verifying", "Image unchanged; checking current container")
        ok, detail = verify_container(
            name,
            expected_image_id=old_image_id,
            was_running=was_running,
            was_paused=was_paused,
            progress=progress,
        )
        if not ok:
            return False, detail, False
        return True, (
            f"{name}: already using image {_short_id(old_image_id)} "
            f"({pull_msg}); {detail}"
        ), False

    host_cfg = dict(host)
    custom_nets = [
        n for n in networks
        if n not in ("bridge", "host", "none", "default")
    ]
    network_mode = str(host_cfg.get("NetworkMode") or "")
    create_kwargs: dict = {
        "name": name,
        "image": image,
        "host_config": host_cfg,
        "environment": cfg.get("Env"),
        "command": cfg.get("Cmd"),
        "entrypoint": cfg.get("Entrypoint"),
        "labels": cfg.get("Labels"),
        "working_dir": cfg.get("WorkingDir"),
        "user": cfg.get("User"),
        "hostname": cfg.get("Hostname"),
        "domainname": cfg.get("Domainname"),
        "tty": cfg.get("Tty", False),
        "stdin_open": cfg.get("OpenStdin", False),
        "stdin_once": cfg.get("StdinOnce", False),
        "volumes": list((cfg.get("Volumes") or {}).keys()),
        "healthcheck": cfg.get("Healthcheck"),
        "stop_signal": cfg.get("StopSignal"),
    }

    if custom_nets and not network_mode.startswith("container:"):
        # Re-attach with the original aliases / static IPs. NetworkMode has to
        # be removed, not set to null, or Docker rejects the create call.
        host_cfg.pop("NetworkMode", None)
        netcfg = {}
        for net_name in custom_nets:
            endpoint = networks[net_name] or {}
            piece = {}
            for key in ("IPAMConfig", "Links", "Aliases", "MacAddress"):
                if endpoint.get(key):
                    piece[key] = endpoint[key]
            netcfg[net_name] = piece
        create_kwargs["networking_config"] = netcfg

    create_kwargs = _supported_kwargs(api.create_container, create_kwargs)

    def rollback(reason: str) -> tuple[bool, str, bool]:
        rb_ok, rb_msg = restore_container(
            name, create_kwargs, old_image_id, was_running, was_paused,
            progress=progress,
        )
        suffix = rb_msg if rb_ok else f"ROLLBACK FAILED: {rb_msg}"
        return False, f"{reason}; {suffix}", False

    if name == SELF_NAME:
        ok, msg = _delegate_self_update(
            info, name, image, old_image_id, new_image_id,
            create_kwargs, was_running, was_paused, job_id=job_id,
            progress=progress,
        )
        return ok, msg, ok

    _report(progress, "recreating", f"Recreating {name}")
    ok, error = recreate_container(create_kwargs, was_running, was_paused)
    if not ok:
        return rollback(f"{name}: recreate failed: {error}")

    if was_running:
        _report(progress, "starting", f"Started {name}")

    try:
        current_info = api.inspect_container(name) or {}
    except Exception as exc:
        return rollback(f"{name}: cannot inspect new container: {exc}")
    current_image_id = current_info.get("Image") or ""
    if new_image_id and current_image_id != new_image_id:
        return rollback(
            f"{name}: container uses {_short_id(current_image_id)} "
            f"instead of pulled image {_short_id(new_image_id)}"
        )

    baseline = _restart_count(name, 0)
    ok, verify_detail = verify_container(
        name,
        expected_image_id=new_image_id or current_image_id,
        was_running=was_running,
        was_paused=was_paused,
        progress=progress,
        baseline_restarts=baseline,
    )
    if not ok:
        return rollback(verify_detail)
    _report(progress, "verifying", verify_detail)

    _report(progress, "cleanup", "Switching to the pulled image and removing the old image")
    cleanup = _cleanup_old_image(old_image_id, name, new_image_id)
    detail = (
        f"{name}: updated to image {_short_id(current_image_id or new_image_id)} "
        f"({pull_msg}); {verify_detail}"
    )
    if cleanup:
        detail += f"; {cleanup}"
    return True, detail, False


def select_targets(mode: str, explicit: list[str] | None = None) -> list[str]:
    """Resolve the containers a schedule should act on."""
    containers = list_containers()
    by_name = {c["name"]: c for c in containers}

    if explicit:
        names = [n for n in explicit if n in by_name]
    elif mode == "selected":
        names = [c["name"] for c in containers if c["update_label"] is True]
    else:
        names = [c["name"] for c in containers if not c["opted_out"]]

    return [
        n for n in names
        if by_name[n]["running"] and not by_name[n]["opted_out"]
    ]


def apply_updates(
    names: list[str], actor: str = "manual"
) -> list[tuple[str, bool, str]]:
    results = []
    for name in names:
        ok, msg, _delegated = update_container(name)
        results.append((name, ok, msg))
        db_side_effect(actor, name, ok, msg)
    return results


def db_side_effect(actor: str, name: str, ok: bool, msg: str) -> None:
    # Imported lazily to avoid a circular import at module load time.
    from . import db

    db.log_action(actor, "update", f"{name}: {'ok' if ok else 'failed'} - {msg}")


# --------------------------------------------------------------------------- images
def list_images() -> list[dict]:
    usage: dict[str, int] = {}
    for container in client.containers.list(all=True):
        image_id = (container.attrs or {}).get("Image") or ""
        if image_id:
            usage[image_id] = usage.get(image_id, 0) + 1

    result = []
    for image in client.images.list(all=True):
        attrs = image.attrs or {}
        image_id = image.id
        result.append(
            {
                "id": image_id,
                "short_id": image_id.replace("sha256:", "")[:12],
                "tags": attrs.get("RepoTags") or [],
                "size": attrs.get("Size", 0),
                "created": attrs.get("Created", ""),
                "containers": usage.get(image_id, 0),
            }
        )
    return sorted(result, key=lambda x: (x["tags"] or [""])[0])


def _find_image(identifier: str):
    identifier = identifier.replace("sha256:", "")
    for candidate in client.images.list(all=True):
        candidate_id = candidate.id.replace("sha256:", "")
        if (
            candidate_id == identifier
            or candidate_id.startswith(identifier)
            or identifier in (candidate.tags or [])
        ):
            return candidate
    return None


def delete_image(identifier: str) -> str:
    image = _find_image(identifier)
    if not image:
        raise ValueError("image not found")

    users = api.containers(all=True, filters={"ancestor": image.id})
    if users:
        names = ", ".join(c.get("Names", ["?"])[0].lstrip("/") for c in users)
        raise RuntimeError(f"image is in use by: {names}")

    api.remove_image(image.id, force=False)
    return f"image {image.id.replace('sha256:', '')[:12]} deleted"


# ------------------------------------------------------------------------- networks
def list_networks() -> list[dict]:
    usage: dict[str, int] = {}
    for container in client.containers.list(all=True):
        nets = ((container.attrs or {}).get("NetworkSettings") or {}).get("Networks") or {}
        for name in nets:
            usage[name] = usage.get(name, 0) + 1

    result = []
    for network in client.networks.list():
        attrs = network.attrs or {}
        ipam = attrs.get("IPAM") or {}
        subnets = [c.get("Subnet", "") for c in (ipam.get("Config") or []) if c.get("Subnet")]
        result.append(
            {
                "id": network.id,
                "short_id": network.id[:12],
                "name": network.name,
                "driver": attrs.get("Driver", ""),
                "scope": attrs.get("Scope", ""),
                "subnets": subnets,
                "containers": usage.get(network.name, 0),
                "system": network.name in ("bridge", "host", "none"),
            }
        )
    return sorted(result, key=lambda x: x["name"])


def _find_network(identifier: str):
    for candidate in client.networks.list():
        if (
            candidate.id == identifier
            or candidate.id.startswith(identifier)
            or candidate.name == identifier
        ):
            return candidate
    return None


def delete_network(identifier: str) -> str:
    network = _find_network(identifier)
    if not network:
        raise ValueError("network not found")
    if network.name in ("bridge", "host", "none"):
        raise RuntimeError(f"system network '{network.name}' cannot be deleted")

    info = api.inspect_network(network.id)
    containers = info.get("Containers") or {}
    if containers:
        names = ", ".join(v.get("Name", "?") for v in containers.values())
        raise RuntimeError(f"network is in use by: {names}")

    api.remove_network(network.id)
    return f"network '{network.name}' deleted"


# ------------------------------------------------------------------------- details
def container_details(name: str) -> dict:
    container = client.containers.get(name)
    attrs = container.attrs or {}
    config = attrs.get("Config") or {}
    host = attrs.get("HostConfig") or {}
    state = attrs.get("State") or {}
    settings = attrs.get("NetworkSettings") or {}
    image_info = {}
    try:
        if attrs.get("Image"):
            image_info = api.inspect_image(attrs["Image"]) or {}
    except Exception:
        pass
    networks = []
    for net_name, endpoint in (settings.get("Networks") or {}).items():
        networks.append(
            {
                "name": net_name,
                "ipv4": endpoint.get("IPAddress", ""),
                "ipv6": endpoint.get("GlobalIPv6Address", ""),
                "mac": endpoint.get("MacAddress", ""),
                "aliases": endpoint.get("Aliases") or [],
            }
        )
    return {
        "name": container.name,
        "id": container.id,
        "short_id": container.id[:12],
        "status": container.status,
        "state": state,
        "health": (state.get("Health") or {}).get("Status"),
        "image": config.get("Image", ""),
        "image_id": attrs.get("Image", ""),
        "image_labels": (image_info.get("Config") or {}).get("Labels") or {},
        "image_digests": image_info.get("RepoDigests") or [],
        "created": attrs.get("Created", ""),
        "command": config.get("Cmd"),
        "entrypoint": config.get("Entrypoint"),
        "environment": config.get("Env") or [],
        "labels": config.get("Labels") or {},
        "mounts": attrs.get("Mounts") or [],
        "networks": networks,
        "ports": settings.get("Ports") or {},
        "restart_policy": host.get("RestartPolicy") or {},
        "privileged": bool(host.get("Privileged")),
        "memory": host.get("Memory", 0),
        "log_config": host.get("LogConfig") or {},
        "is_self": container.name == SELF_NAME,
    }


_LOG_TIMESTAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}T[^\s]+Z)\s*")


def _decode_logs(raw, tty: bool) -> str:
    """Decode Docker's multiplexed log frames into plain text."""
    if isinstance(raw, str):
        return raw
    data = bytes(raw or b"")
    if tty:
        return data.decode("utf-8", "replace")

    chunks = bytearray()
    pos = 0
    while pos + 8 <= len(data):
        if data[pos] not in (1, 2):
            return data.decode("utf-8", "replace")
        size = int.from_bytes(data[pos + 4:pos + 8], "big")
        if pos + 8 + size > len(data):
            return data.decode("utf-8", "replace")
        chunks += data[pos + 8:pos + 8 + size]
        pos += 8 + size
    if pos < len(data):
        chunks += data[pos:]
    return chunks.decode("utf-8", "replace")


def _format_log_line(line: str) -> str:
    match = _LOG_TIMESTAMP.match(line)
    if not match:
        return line
    return f"{timefmt.format_time(match.group(1))} {line[match.end():]}"


def container_logs(name: str, tail: int = 200) -> list[str]:
    """Return recent container log lines with timestamps in the configured TZ."""
    container = get_container(name)
    tty = bool(((container.attrs or {}).get("Config") or {}).get("Tty"))
    raw = api.logs(
        container.id,
        stdout=True,
        stderr=True,
        stream=False,
        timestamps=True,
        tail=str(max(1, min(1000, tail))),
    )
    text = _decode_logs(raw, tty)
    return [_format_log_line(line)[:4000] for line in text.splitlines()[-tail:]]


def image_details(identifier: str) -> dict:
    image = _find_image(identifier)
    if not image:
        raise ValueError("image not found")
    attrs = image.attrs or {}
    config = attrs.get("Config") or {}
    users = api.containers(all=True, filters={"ancestor": image.id})
    return {
        "id": image.id,
        "short_id": image.id.replace("sha256:", "")[:12],
        "tags": attrs.get("RepoTags") or [],
        "created": attrs.get("Created", ""),
        "size": attrs.get("Size", 0),
        "architecture": attrs.get("Architecture", ""),
        "os": attrs.get("Os", ""),
        "docker_version": attrs.get("DockerVersion", ""),
        "environment": config.get("Env") or [],
        "labels": config.get("Labels") or {},
        "layers": (attrs.get("RootFS") or {}).get("Layers") or [],
        "command": config.get("Cmd"),
        "entrypoint": config.get("Entrypoint"),
        "users": [
            (c.get("Names") or ["?"])[0].lstrip("/") for c in users
        ],
    }


def network_details(identifier: str) -> dict:
    network = _find_network(identifier)
    if not network:
        raise ValueError("network not found")
    info = api.inspect_network(network.id)
    ipam = info.get("IPAM") or {}
    subnets = [
        {
            "subnet": c.get("Subnet", ""),
            "gateway": c.get("Gateway", ""),
            "ip_range": c.get("IPRange", ""),
        }
        for c in (ipam.get("Config") or [])
    ]
    attached = []
    for container_id, endpoint in (info.get("Containers") or {}).items():
        attached.append(
            {
                "id": container_id[:12],
                "name": endpoint.get("Name", ""),
                "ipv4": endpoint.get("IPv4Address", ""),
                "ipv6": endpoint.get("IPv6Address", ""),
                "mac": endpoint.get("MacAddress", ""),
            }
        )
    return {
        "id": network.id,
        "short_id": network.id[:12],
        "name": network.name,
        "driver": info.get("Driver", ""),
        "scope": info.get("Scope", ""),
        "internal": bool(info.get("Internal")),
        "attachable": bool(info.get("Attachable")),
        "labels": info.get("Labels") or {},
        "options": info.get("Options") or {},
        "subnets": subnets,
        "containers": attached,
        "system": network.name in ("bridge", "host", "none"),
    }
