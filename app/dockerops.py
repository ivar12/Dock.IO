"""Docker operations: inventory, start/stop/restart and in-place image updates."""

from __future__ import annotations

import inspect
import os

import docker

client = docker.from_env()
api = client.api

SELF_NAME = os.getenv("SELF_CONTAINER", "dockio")
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
        try:
            if c.image and c.image.tags:
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


def _pull(image: str) -> tuple[bool, str]:
    repo, tag = _split_image(image)
    last_line = ""
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
        return True, last_line or "image pulled"
    except Exception as exc:  # docker.errors.APIError, image not found, registry down, ...
        return False, f"pull failed: {exc}"


def _supported_kwargs(func, kwargs: dict) -> dict:
    params = inspect.signature(func).parameters
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return kwargs
    return {k: v for k, v in kwargs.items() if k in params}


def _cleanup_old_image(old_image_id: str, container_name: str) -> str:
    """Remove the superseded image if nothing else uses it."""
    if not old_image_id:
        return ""
    try:
        new_info = api.inspect_container(container_name)
        if new_info.get("Image") == old_image_id:
            return "old image unchanged"
    except Exception:
        return "could not verify new image"
    try:
        users = api.containers(all=True, filters={"ancestor": old_image_id})
        if users:
            return f"old image kept ({len(users)} container(s) still use it)"
        api.remove_image(old_image_id, force=False)
        return f"old image {old_image_id[:12]} removed"
    except Exception as exc:
        return f"old image cleanup skipped: {exc}"


def update_container(name: str) -> tuple[bool, str]:
    """Pull the configured image and recreate the container with the same config."""
    try:
        info = api.inspect_container(name)
    except Exception as exc:
        return False, f"{name}: inspect failed: {exc}"

    old_image_id = info.get("Image") or ""

    cfg = info.get("Config") or {}
    host = info.get("HostConfig") or {}
    networks = info.get("NetworkSettings", {}).get("Networks", {}) or {}
    image = cfg.get("Image") or ""
    if not image:
        return False, f"{name}: container has no image"

    ok, pull_msg = _pull(image)
    if not ok:
        return False, f"{name}: {pull_msg}"

    state = info.get("State") or {}
    was_running = bool(state.get("Running"))

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
        # Re-attach with the original aliases / static IPs instead of relying on NetworkMode.
        host_cfg["NetworkMode"] = None
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

    try:
        api.remove_container(name, force=True)
        created = api.create_container(**create_kwargs)
        container_id = created["Id"]
        if was_running:
            api.start(container_id)
    except Exception as exc:
        return False, f"{name}: recreate failed: {exc}"

    cleanup = _cleanup_old_image(old_image_id, name)
    detail = f"{name}: updated ({pull_msg})"
    if cleanup:
        detail += f"; {cleanup}"
    return True, detail


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

    return [n for n in names if n != SELF_NAME and by_name[n]["running"]]


def apply_updates(
    names: list[str], actor: str = "manual"
) -> list[tuple[str, bool, str]]:
    results = []
    for name in names:
        ok, msg = update_container(name)
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
