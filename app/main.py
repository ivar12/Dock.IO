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

from . import auth, db, dockerops, scheduler

SESSION_SECRET = os.getenv("SESSION_SECRET", "")
if not SESSION_SECRET:
    raise RuntimeError("SESSION_SECRET is not set in .env")
SESSION_HTTPS_ONLY = os.getenv("SESSION_HTTPS_ONLY", "true").lower() in ("1", "true", "yes")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    auth.init_local_admin()
    scheduler.start()
    db.log_action("system", "startup", "docker manager started")
    yield
    scheduler.stop()


app = FastAPI(title="Docker Manager", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    https_only=SESSION_HTTPS_ONLY,
    same_site="lax",
    session_cookie="dockermgr_session",
)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


def _user(request: Request) -> str | None:
    return request.session.get("user")


def _need_login(request: Request) -> RedirectResponse | None:
    if not _user(request):
        return RedirectResponse("/login", status_code=302)
    return None


# --------------------------------------------------------------------------- auth
@app.get("/health")
def health():
    return {"ok": True}


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
                f"{c['name']} has invalid dockermgr.update label "
                f"('{c['labels'].get('dockermgr.update')}')"
            )

    stats = {
        "total": len(containers),
        "running": sum(c["running"] for c in containers),
        "stopped": sum(not c["running"] for c in containers),
        "unhealthy": sum(c["health"] == "unhealthy" for c in containers),
        "all_schedule": sum(
            not c["is_self"] and not c["opted_out"] and not c["update_label"]
            for c in containers
        ),
        "selected": sum(
            1
            for c in containers
            if not c["is_self"] and c["update_label"] is True and not c["opted_out"]
        ),
        "opted_out": sum(c["opted_out"] for c in containers),
        "issues": issues,
    }

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"user": _user(request), "containers": containers, "stats": stats},
    )


@app.post("/container/{name}/action")
def container_action(request: Request, name: str, act: str = Form(...)):
    if redirect := _need_login(request):
        return redirect
    try:
        msg = dockerops.action(name, act)
    except Exception as exc:
        msg = f"{name}: {act} failed - {exc}"
    db.log_action(_user(request), f"{act} container", msg)
    return RedirectResponse("/", status_code=303)


@app.post("/container/{name}/update")
def container_update(request: Request, name: str):
    if redirect := _need_login(request):
        return redirect
    ok, msg = dockerops.update_container(name)
    db.log_action(_user(request), "manual update", f"{'ok' if ok else 'failed'}: {msg}")
    return RedirectResponse("/", status_code=303)


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
    try:
        CronTrigger.from_crontab(cron)
    except ValueError as exc:
        db.log_action(_user(request), "schedule rejected", f"{cron}: {exc}")
        return RedirectResponse("/schedules?error=Invalid+cron+expression", status_code=303)
    schedule_id = db.create_schedule(name, cron, mode, containers, enabled == "on")
    scheduler.refresh_jobs()
    db.log_action(_user(request), "create schedule", f"{name} ({cron}, {mode})")
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
        background_tasks.add_task(scheduler.run_schedule, schedule_id)
        db.log_action(_user(request), "run schedule now", schedule["name"])
    return RedirectResponse("/schedules", status_code=303)


@app.get("/logs")
def logs_page(request: Request):
    if redirect := _need_login(request):
        return redirect
    return templates.TemplateResponse(
        request,
        "logs.html",
        {"user": _user(request), "entries": db.list_audit(200)},
    )


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
