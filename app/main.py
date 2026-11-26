"""Custom Docker manager webapp."""

from __future__ import annotations

import os
import urllib.parse
from contextlib import asynccontextmanager

from apscheduler.triggers.cron import CronTrigger
from fastapi import BackgroundTasks, FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import auth, db, dockerops, image_updates, scheduler, timefmt, updates, version

SESSION_SECRET = os.getenv("SESSION_SECRET", "")
if not SESSION_SECRET:
    raise RuntimeError("SESSION_SECRET is not set in .env")
SESSION_HTTPS_ONLY = os.getenv("SESSION_HTTPS_ONLY", "true").lower() in ("1", "true", "yes")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    updates.reconcile_after_restart()
    scheduler.start()
    version.refresh()
    image_updates.refresh()
    db.log_action("system", "startup", f"Dock.IO {version.VERSION} started")
    yield
    scheduler.stop()


app = FastAPI(title="Dock.IO", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    https_only=SESSION_HTTPS_ONLY,
    same_site="lax",
    session_cookie="dockio_session",
)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["app_version"] = version.VERSION
templates.env.globals["release_info"] = version.update_info
templates.env.globals["app_timezone"] = timefmt.timezone_label()
templates.env.filters["localtime"] = timefmt.format_time


@app.middleware("http")
async def setup_guard(request: Request, call_next):
    """On a fresh install, send every page to the first-run admin setup."""
    path = request.url.path
    if path not in ("/setup", "/health") and not path.startswith("/static/"):
        if db.user_count() == 0:
            return RedirectResponse("/setup", status_code=302)
    return await call_next(request)


def _user(request: Request) -> str | None:
    return request.session.get("user")


def _need_login(request: Request) -> RedirectResponse | None:
    if not _user(request):
        return RedirectResponse("/login", status_code=302)
    return None


def _need_admin(request: Request) -> RedirectResponse | None:
    if redirect := _need_login(request):
        return redirect
    username = _user(request)
    row = db.get_user(username) if username else None
    if not row or row["role"] != "admin":
        return RedirectResponse("/?error=Admin+access+required", status_code=302)
    return None


# --------------------------------------------------------------------------- auth
@app.get("/health")
def health():
    return {"ok": True}


# ------------------------------------------------------------------------ first run
@app.get("/setup")
def setup_page(request: Request, error: str = ""):
    if db.user_count():
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(request, "setup.html", {"error": error})


@app.post("/setup")
def setup_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
):
    if db.user_count():
        return RedirectResponse("/login", status_code=302)
    username = username.strip()
    if not username or len(username) > 64:
        return templates.TemplateResponse(
            request, "setup.html", {"error": "Username must be 1-64 characters"}, status_code=400
        )
    if password != confirm_password:
        return templates.TemplateResponse(
            request, "setup.html", {"error": "Passwords do not match"}, status_code=400
        )
    if len(password) < 8:
        return templates.TemplateResponse(
            request, "setup.html", {"error": "Password must be at least 8 characters"}, status_code=400
        )
    if not db.create_initial_admin(username, auth.hash_password(password)):
        return RedirectResponse("/login", status_code=302)
    request.session["user"] = username
    db.log_action(username, "first-run setup", "created the first admin account")
    return RedirectResponse("/", status_code=303)


@app.get("/login")
def login_page(request: Request, error: str = ""):
    if _user(request):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"oidc": auth.oidc_public_config(), "error": error},
    )


@app.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    if auth.check_local_login(username, password):
        request.session["user"] = username
        db.log_action(username, "login", "local")
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"oidc": auth.oidc_public_config(), "error": "Invalid username or password"},
        status_code=401,
    )


@app.get("/auth/oidc")
def oidc_start(request: Request):
    if not auth.OIDC_ENABLED:
        return RedirectResponse("/login?error=OIDC+is+disabled", status_code=302)
    try:
        url, state = auth.authorization_url()
    except Exception as exc:
        return RedirectResponse(f"/login?error={exc}", status_code=302)
    request.session["oidc_state"] = state
    return RedirectResponse(url, status_code=302)


@app.get("/auth/callback")
def oidc_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    expected = request.session.pop("oidc_state", None)
    if error or not code or not state or not expected or state != expected:
        return RedirectResponse("/login?error=OIDC+callback+failed", status_code=302)
    try:
        username = auth.exchange_code_and_get_user(code)
    except Exception as exc:
        db.log_action("oidc", "callback failed", str(exc)[:500])
        return RedirectResponse("/login?error=OIDC+exchange+failed", status_code=302)
    db.ensure_oidc_user(username)
    request.session["user"] = username
    db.log_action(username, "login", "oidc")
    return RedirectResponse("/", status_code=302)


@app.get("/auth/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


# ------------------------------------------------------------------------- pages
@app.get("/")
def dashboard(request: Request):
    if redirect := _need_login(request):
        return redirect
    containers = dockerops.list_containers()
    image_summary = image_updates.summary(containers)
    schedule_coverage = scheduler.coverage(containers)
    for container in containers:
        container["image_version"] = image_updates.image_version(
            container.get("configured_image") or container.get("image") or ""
        )
        container["image_update"] = image_summary["states"].get(
            container["name"], image_updates.UNKNOWN
        )
        covered = schedule_coverage[container["name"]]
        container["schedules"] = covered["schedules"]
        if container["opted_out"]:
            container["update_filter"] = "opted-out"
            container["update_text"] = "opted out"
        elif not covered["scheduled"]:
            container["update_filter"] = "no-schedule"
            container["update_text"] = "no schedule"
        elif "selected" in covered["modes"]:
            container["update_filter"] = "selected"
            container["update_text"] = ", ".join(dict.fromkeys(covered["schedules"]))
        else:
            container["update_filter"] = "all"
            container["update_text"] = ", ".join(dict.fromkeys(covered["schedules"]))

    issues = []
    for c in containers:
        if not c["running"]:
            issues.append(f"{c['name']} is {c['status']}")
        if c["health"] == "unhealthy":
            issues.append(f"{c['name']} health check is unhealthy")
        if c["image_missing"]:
            issues.append(f"{c['name']} has no image configured")
        if c["invalid_update_label"]:
            issues.append(
                f"{c['name']} has invalid dockio.update label "
                f"('{c['labels'].get('dockio.update')}')"
            )

    stats = {
        "total": len(containers),
        "running": sum(c["running"] for c in containers),
        "stopped": sum(not c["running"] for c in containers),
        "unhealthy": sum(c["health"] == "unhealthy" for c in containers),
        "scheduled": sum(c["update_filter"] in ("selected", "all") for c in containers),
        "no_schedule": sum(c["update_filter"] == "no-schedule" for c in containers),
        "opted_out": sum(c["update_filter"] == "opted-out" for c in containers),
        "updates_available": image_summary["available"],
        "checking_updates": image_summary["busy"],
        "issues": issues,
    }

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": _user(request),
            "containers": containers,
            "stats": stats,
            "msg": request.query_params.get("msg", ""),
            "error": request.query_params.get("error", ""),
        },
    )


# ------------------------------------------------------- container list tools
@app.get("/containers/update-status")
def containers_update_status(request: Request):
    if not _user(request):
        return {"error": "not authenticated"}, 401
    return image_updates.summary(dockerops.list_containers())


@app.post("/containers/check-updates")
def containers_check_updates(request: Request):
    if redirect := _need_login(request):
        return redirect
    image_updates.refresh(force=True)
    db.log_action(_user(request), "check image updates", "all containers")
    return RedirectResponse("/", status_code=303)


@app.post("/containers/bulk/action")
def bulk_container_action(
    request: Request, action: str = Form(...), names: list[str] = Form([])
):
    if redirect := _need_login(request):
        return redirect
    actor = _user(request) or "unknown"
    if action not in ("start", "stop", "restart", "update"):
        return RedirectResponse("/?error=Unknown+bulk+action", status_code=303)

    ok: list[str] = []
    failed: list[str] = []
    for name in names:
        if action == "update":
            updates.start_update(name, actor=actor)
            ok.append(f"{name}: update started")
            continue
        try:
            ok.append(dockerops.action(name, action))
        except Exception as exc:
            failed.append(f"{name}: {exc}")

    db.log_action(actor, "bulk container action", f"{action} on {len(names)} container(s)")
    if action == "update" and ok:
        return RedirectResponse("/updates", status_code=303)
    text = "; ".join(ok + failed) or "No containers selected"
    kind = "error" if failed else "msg"
    return RedirectResponse(f"/?{kind}={urllib.parse.quote(text)}", status_code=303)


def _safe_next(next_url: str, fallback: str) -> str:
    """Keep post-action redirects on the page the user came from."""
    if next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return fallback


def _valid_cron(cron: str) -> bool:
    try:
        CronTrigger.from_crontab(cron)
        return True
    except ValueError:
        return False


@app.post("/container/{name}/action")
def container_action(
    request: Request, name: str, act: str = Form(...), next: str = Form("")
):
    if redirect := _need_login(request):
        return redirect
    try:
        msg = dockerops.action(name, act)
    except Exception as exc:
        msg = f"{name}: {act} failed - {exc}"
    db.log_action(_user(request), f"{act} container", msg)
    return RedirectResponse(_safe_next(next, "/"), status_code=303)


@app.post("/container/{name}/update")
def container_update(request: Request, name: str, next: str = Form("")):
    if redirect := _need_login(request):
        return redirect
    updates.start_update(name, actor=_user(request) or "unknown")
    return RedirectResponse(_safe_next(next, "/updates"), status_code=303)


@app.get("/schedules")
def schedules_page(request: Request):
    if redirect := _need_login(request):
        return redirect
    return templates.TemplateResponse(
        request,
        "schedules.html",
        {
            "user": _user(request),
            "schedules": db.list_schedules(),
            "containers": [c["name"] for c in dockerops.list_containers()],
        },
    )


@app.get("/schedules/{schedule_id}")
def schedule_detail_page(request: Request, schedule_id: int):
    if redirect := _need_login(request):
        return redirect
    schedule = db.get_schedule(schedule_id)
    if not schedule:
        return RedirectResponse("/schedules?error=Schedule+not+found", status_code=302)
    return templates.TemplateResponse(
        request,
        "schedule_detail.html",
        {"user": _user(request), "schedule": schedule},
    )


@app.post("/schedules")
def create_schedule(
    request: Request,
    name: str = Form(...),
    cron: str = Form(...),
    mode: str = Form(...),
    enabled: str = Form("on"),
    containers: list[str] = Form([]),
):
    if redirect := _need_login(request):
        return redirect
    if mode not in ("all", "selected"):
        mode = "all"
    if not _valid_cron(cron):
        db.log_action(_user(request), "schedule rejected", f"invalid cron: {cron}")
        return RedirectResponse("/schedules?error=Invalid+cron+expression", status_code=303)
    schedule_id = db.create_schedule(name, cron, mode, containers, enabled == "on")
    scheduler.refresh_jobs()
    db.log_action(_user(request), "create schedule", f"{name} ({cron}, {mode})")
    return RedirectResponse("/schedules", status_code=303)


@app.get("/schedules/{schedule_id}/edit")
def edit_schedule_page(request: Request, schedule_id: int):
    if redirect := _need_login(request):
        return redirect
    schedule = db.get_schedule(schedule_id)
    if not schedule:
        return RedirectResponse("/schedules?error=Schedule+not+found", status_code=302)
    return templates.TemplateResponse(
        request,
        "schedule_edit.html",
        {
            "user": _user(request),
            "schedule": schedule,
            "containers": [c["name"] for c in dockerops.list_containers()],
        },
    )


@app.post("/schedules/{schedule_id}/edit")
def edit_schedule(
    request: Request,
    schedule_id: int,
    name: str = Form(...),
    cron: str = Form(...),
    mode: str = Form(...),
    enabled: str = Form("on"),
    containers: list[str] = Form([]),
):
    if redirect := _need_login(request):
        return redirect
    schedule = db.get_schedule(schedule_id)
    if not schedule:
        return RedirectResponse("/schedules?error=Schedule+not+found", status_code=302)
    if mode not in ("all", "selected"):
        mode = "all"
    if not _valid_cron(cron):
        return RedirectResponse(
            f"/schedules/{schedule_id}/edit?error=Invalid+cron+expression", status_code=303
        )
    db.update_schedule(
        schedule_id,
        name=name,
        cron=cron,
        mode=mode,
        containers=containers,
        enabled=enabled == "on",
    )
    scheduler.refresh_jobs()
    db.log_action(_user(request), "edit schedule", f"{schedule['name']} -> {name} ({cron}, {mode})")
    return RedirectResponse("/schedules", status_code=303)


@app.post("/schedules/{schedule_id}/toggle")
def toggle_schedule(request: Request, schedule_id: int):
    if redirect := _need_login(request):
        return redirect
    schedule = db.get_schedule(schedule_id)
    if schedule:
        db.update_schedule(schedule_id, enabled=not schedule["enabled"])
        scheduler.refresh_jobs()
        db.log_action(_user(request), "toggle schedule", f"{schedule['name']}")
    return RedirectResponse("/schedules", status_code=303)


@app.post("/schedules/{schedule_id}/delete")
def delete_schedule(request: Request, schedule_id: int):
    if redirect := _need_login(request):
        return redirect
    schedule = db.get_schedule(schedule_id)
    if schedule:
        scheduler.remove_job(schedule_id)
        db.delete_schedule(schedule_id)
        db.log_action(_user(request), "delete schedule", schedule["name"])
    return RedirectResponse("/schedules", status_code=303)


@app.post("/schedules/{schedule_id}/run")
def run_schedule_now(request: Request, schedule_id: int, background_tasks: BackgroundTasks):
    if redirect := _need_login(request):
        return redirect
    schedule = db.get_schedule(schedule_id)
    if schedule:
        actor = _user(request) or "unknown"
        background_tasks.add_task(scheduler.run_schedule, schedule_id, actor)
        db.log_action(actor, "run schedule now", schedule["name"])
    return RedirectResponse("/updates", status_code=303)


@app.get("/logs")
def logs_page(request: Request):
    if redirect := _need_login(request):
        return redirect
    return templates.TemplateResponse(
        request,
        "logs.html",
        {"user": _user(request), "entries": db.list_audit(200)},
    )


# ----------------------------------------------------------------------- updates
@app.get("/updates")
def updates_page(request: Request):
    if redirect := _need_login(request):
        return redirect
    return templates.TemplateResponse(
        request,
        "updates.html",
        {
            "user": _user(request),
            "update_info": version.update_info(),
            "phases": updates.PHASE_LABELS,
            "history_hours": updates.JOB_HISTORY_HOURS,
        },
    )


@app.get("/updates/status")
def updates_status(request: Request):
    if not _user(request):
        return {"error": "not authenticated"}, 401
    return updates.status()


@app.post("/updates/check")
def updates_check(request: Request):
    if redirect := _need_login(request):
        return redirect
    version.check_now()
    return RedirectResponse("/updates", status_code=303)


# ---------------------------------------------------------------------- images
@app.get("/images")
def images_page(request: Request):
    if redirect := _need_login(request):
        return redirect
    return templates.TemplateResponse(
        request,
        "images.html",
        {
            "user": _user(request),
            "images": dockerops.list_images(),
            "msg": request.query_params.get("msg", ""),
            "error": request.query_params.get("error", ""),
        },
    )


@app.post("/images/{image_id:path}/delete")
def image_delete(request: Request, image_id: str):
    if redirect := _need_login(request):
        return redirect
    try:
        msg = dockerops.delete_image(image_id)
        db.log_action(_user(request), "delete image", msg)
        target = f"/images?msg={urllib.parse.quote(msg)}"
    except Exception as exc:
        db.log_action(_user(request), "delete image failed", str(exc))
        target = f"/images?error={urllib.parse.quote(str(exc))}"
    return RedirectResponse(target, status_code=303)


@app.post("/images/bulk/delete")
def bulk_images_delete(request: Request, ids: list[str] = Form([])):
    if redirect := _need_login(request):
        return redirect
    ok: list[str] = []
    failed: list[str] = []
    for image_id in ids:
        try:
            ok.append(dockerops.delete_image(image_id))
        except Exception as exc:
            failed.append(f"{image_id}: {exc}")
    db.log_action(_user(request), "bulk delete images", ", ".join(ids) or "none")
    text = "; ".join(ok + failed) or "No images selected"
    kind = "error" if failed else "msg"
    return RedirectResponse(f"/images?{kind}={urllib.parse.quote(text)}", status_code=303)


# -------------------------------------------------------------------- networks
@app.get("/networks")
def networks_page(request: Request):
    if redirect := _need_login(request):
        return redirect
    return templates.TemplateResponse(
        request,
        "networks.html",
        {
            "user": _user(request),
            "networks": dockerops.list_networks(),
            "msg": request.query_params.get("msg", ""),
            "error": request.query_params.get("error", ""),
        },
    )


@app.post("/networks/{network_id}/delete")
def network_delete(request: Request, network_id: str):
    if redirect := _need_login(request):
        return redirect
    try:
        msg = dockerops.delete_network(network_id)
        db.log_action(_user(request), "delete network", msg)
        target = f"/networks?msg={urllib.parse.quote(msg)}"
    except Exception as exc:
        db.log_action(_user(request), "delete network failed", str(exc))
        target = f"/networks?error={urllib.parse.quote(str(exc))}"
    return RedirectResponse(target, status_code=303)


@app.post("/networks/bulk/delete")
def bulk_networks_delete(request: Request, ids: list[str] = Form([])):
    if redirect := _need_login(request):
        return redirect
    ok: list[str] = []
    failed: list[str] = []
    for network_id in ids:
        try:
            ok.append(dockerops.delete_network(network_id))
        except Exception as exc:
            failed.append(f"{network_id}: {exc}")
    db.log_action(_user(request), "bulk delete networks", ", ".join(ids) or "none")
    text = "; ".join(ok + failed) or "No networks selected"
    kind = "error" if failed else "msg"
    return RedirectResponse(f"/networks?{kind}={urllib.parse.quote(text)}", status_code=303)


# ------------------------------------------------------------------------ detail views
@app.get("/container/{name}")
def container_detail(request: Request, name: str):
    if redirect := _need_login(request):
        return redirect
    try:
        detail = dockerops.container_details(name)
    except Exception as exc:
        return RedirectResponse(f"/?error={urllib.parse.quote(str(exc))}", status_code=302)
    detail["image_version"] = image_updates.image_version(detail.get("image") or "")
    return templates.TemplateResponse(
        request,
        "container_detail.html",
        {"user": _user(request), "detail": detail},
    )


@app.get("/container/{name}/logs")
def container_logs(request: Request, name: str, tail: int = 200):
    if not _user(request):
        return {"error": "not authenticated"}, 401
    try:
        lines = dockerops.container_logs(name, tail=max(1, min(1000, tail)))
    except Exception as exc:
        return {"error": str(exc)}, 400
    return {"lines": lines}


@app.get("/images/{image_id:path}")
def image_detail(request: Request, image_id: str):
    if redirect := _need_login(request):
        return redirect
    try:
        detail = dockerops.image_details(image_id)
    except Exception as exc:
        return RedirectResponse(
            f"/images?error={urllib.parse.quote(str(exc))}", status_code=302
        )
    return templates.TemplateResponse(
        request,
        "image_detail.html",
        {"user": _user(request), "detail": detail},
    )


@app.get("/networks/{network_id}")
def network_detail(request: Request, network_id: str):
    if redirect := _need_login(request):
        return redirect
    try:
        detail = dockerops.network_details(network_id)
    except Exception as exc:
        return RedirectResponse(
            f"/networks?error={urllib.parse.quote(str(exc))}", status_code=302
        )
    return templates.TemplateResponse(
        request,
        "network_detail.html",
        {"user": _user(request), "detail": detail},
    )


# --------------------------------------------------------------------------- settings
def _settings_redirect(kind: str, text: str) -> RedirectResponse:
    return RedirectResponse(f"/settings?{kind}={urllib.parse.quote(text)}", status_code=303)


@app.get("/settings")
def settings_page(request: Request):
    if redirect := _need_admin(request):
        return redirect

    try:
        docker_version = dockerops.client.api.version().get("ApiVersion", "")
    except Exception:
        docker_version = ""

    users = db.list_users()
    overview = {
        "users": len(users),
        "admins": sum(u["role"] == "admin" for u in users),
        "local": sum(u["is_local"] for u in users),
        "oidc": sum(not u["is_local"] for u in users),
        "schedules": len(db.list_schedules()),
        "jobs": len(scheduler.scheduler.get_jobs()),
        "oidc_status": "enabled" if auth.OIDC_ENABLED else "disabled",
    }

    release = version.update_info()
    info = [
        {"label": "Running version", "value": f"v{release['current']}"},
        {"label": "Latest available", "value": f"v{release['latest']}" if release["latest"] else "unknown"},
        {"label": "First admin account", "value": "created in the web UI on first launch"},
        {"label": "Session cookie", "value": "HTTPS only" if SESSION_HTTPS_ONLY else "HTTP allowed (local testing)"},
        {"label": "OIDC", "value": "enabled" if auth.OIDC_ENABLED else "disabled"},
        {"label": "OIDC client ID", "value": auth.OIDC_CLIENT_ID or "-"},
        {"label": "OIDC client secret", "value": "configured" if auth.OIDC_CLIENT_SECRET else "not configured"},
        {"label": "OIDC redirect URL", "value": auth.OIDC_REDIRECT_URL},
        {"label": "OIDC authorize URL", "value": auth.OIDC_AUTHORIZE_URL or "-"},
        {"label": "OIDC user claim", "value": auth.OIDC_USER_CLAIM},
        {"label": "Active scheduler jobs", "value": len(scheduler.scheduler.get_jobs())},
        {"label": "Timezone", "value": os.getenv("TZ", "UTC")},
        {"label": "Self container", "value": os.getenv("SELF_CONTAINER", "") or "-"},
        {"label": "Data directory", "value": str(db.DATA_DIR)},
        {"label": "Docker API version", "value": docker_version or "-"},
    ]

    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "user": _user(request),
            "users": users,
            "overview": overview,
            "info": info,
            "msg": request.query_params.get("msg", ""),
            "error": request.query_params.get("error", ""),
        },
    )


@app.post("/settings/users")
def settings_save_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    role: str = Form("user"),
):
    if redirect := _need_admin(request):
        return redirect
    username = username.strip()
    if not username:
        return _settings_redirect("error", "Username is required")
    if password != confirm_password:
        return _settings_redirect("error", "Passwords do not match")
    if len(password) < 8:
        return _settings_redirect("error", "Password must be at least 8 characters")
    if role not in ("admin", "user"):
        role = "user"
    db.upsert_local_user(username, auth.hash_password(password), role=role)
    db.log_action(_user(request), "create user", f"local user {username} ({role})")
    return _settings_redirect("msg", f"User '{username}' saved")


@app.get("/settings/users/{username}/edit")
def settings_edit_user_page(request: Request, username: str):
    if redirect := _need_admin(request):
        return redirect
    row = db.get_user(username)
    if not row:
        return _settings_redirect("error", "User not found")
    target = {
        "username": row["username"],
        "role": row["role"],
        "is_local": bool(row["is_local"]),
        "has_password": bool(row["password_hash"]),
        "created_at": row["created_at"],
    }
    return templates.TemplateResponse(
        request,
        "settings_user_edit.html",
        {
            "user": _user(request),
            "target": target,
            "msg": request.query_params.get("msg", ""),
            "error": request.query_params.get("error", ""),
        },
    )


@app.post("/settings/users/{username}/edit")
def settings_edit_user(
    request: Request,
    username: str,
    role: str = Form("user"),
    password: str = Form(""),
    confirm_password: str = Form(""),
):
    if redirect := _need_admin(request):
        return redirect
    row = db.get_user(username)
    if not row:
        return _settings_redirect("error", "User not found")
    if role not in ("admin", "user"):
        return _settings_redirect("error", "Invalid role")
    if username == _user(request) and role != row["role"]:
        return _settings_redirect("error", "You cannot change your own role")

    changes = [f"role={role}"]
    if password or confirm_password:
        if password != confirm_password:
            return _settings_redirect("error", "Passwords do not match")
        if len(password) < 8:
            return _settings_redirect("error", "Password must be at least 8 characters")
        db.update_user(username, role=role, password_hash=auth.hash_password(password))
        changes.append("password reset")
    else:
        db.update_user(username, role=role)

    db.log_action(_user(request), "edit user", f"{username}: {', '.join(changes)}")
    return _settings_redirect("msg", f"User '{username}' updated ({', '.join(changes)})")


@app.post("/settings/users/{username}/role")
def settings_set_role(request: Request, username: str, role: str = Form(...)):
    if redirect := _need_admin(request):
        return redirect
    if username == _user(request):
        return _settings_redirect("error", "You cannot change your own role")
    if role not in ("admin", "user"):
        return _settings_redirect("error", "Invalid role")
    if not db.set_user_role(username, role):
        return _settings_redirect("error", "User not found")
    db.log_action(_user(request), "set user role", f"{username} -> {role}")
    return _settings_redirect("msg", f"Role for '{username}' set to {role}")


@app.post("/settings/users/{username}/delete")
def settings_delete_user(request: Request, username: str):
    if redirect := _need_admin(request):
        return redirect
    if username == _user(request):
        return _settings_redirect("error", "You cannot delete the account you are logged in with")
    if not db.delete_user(username):
        return _settings_redirect("error", "User not found")
    db.log_action(_user(request), "delete user", username)
    return _settings_redirect("msg", f"User '{username}' deleted")


@app.post("/settings/password")
def settings_change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
):
    if redirect := _need_admin(request):
        return redirect
    username = _user(request)
    row = db.get_user(username)
    if not row or not row["is_local"] or not row["password_hash"]:
        return _settings_redirect("error", "This account has no local password to change")
    if not auth.verify_password(current_password, row["password_hash"]):
        return _settings_redirect("error", "Current password is incorrect")
    if new_password != confirm_password:
        return _settings_redirect("error", "New passwords do not match")
    if len(new_password) < 8:
        return _settings_redirect("error", "New password must be at least 8 characters")
    db.upsert_local_user(username, auth.hash_password(new_password), role=row["role"])
    db.log_action(username, "change own password", "settings")
    return _settings_redirect("msg", "Password changed")
