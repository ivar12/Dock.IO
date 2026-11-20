"""Docker operations: inventory, start/stop/restart and in-place image updates."""

from __future__ import annotations

import inspect
import os

import docker

client = docker.from_env()
api = client.api

SELF_NAME = os.getenv("SELF_CONTAINER", "dockermgr")
OPT_OUT_LABELS = (
    "dockermgr.update",
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

        raw_update_label = labels.get("dockermgr.update")
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
                "update_label": _bool_label(labels, "dockermgr.update"),
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


def update_container(name: str) -> tuple[bool, str]:
    """Pull the configured image and recreate the container with the same config."""
    try:
        info = api.inspect_container(name)
    except Exception as exc:
        return False, f"{name}: inspect failed: {exc}"

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

    return True, f"{name}: updated ({pull_msg})"


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
