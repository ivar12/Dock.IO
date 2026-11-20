# AGENT.md

This file is the handover guide for AI agents and future maintainers working in this repository.

## Project identity

This is a **custom-built Docker manager webapp**, written from scratch in this repo. It is **not** Portainer, Watchtower, or a repackaged off-the-shelf product. Do not suggest or revert to those; the user explicitly wanted a custom application.

The app manages Docker containers, pulls images, recreates containers on update, runs scheduled updates, and provides local + OIDC authentication.

Current released version: **v1.8.6**.

## Golden rules

1. Keep secrets out of git. `.env` is gitignored; `.env.example` is tracked.
2. Never commit `ADMIN_PASSWORD_HASH_B64`, `SESSION_SECRET`, `OIDC_CLIENT_SECRET`, tokens, or database files.
3. `/var/run/docker.sock` is mounted into the app. Treat the app as root-equivalent on the host.
4. Do not attach the app to any network other than `backend`. The `internal` network is declared but intentionally unused.
5. Preserve the user's UI preferences:
   - Row buttons in list pages are **Show**, not Edit.
   - Titles and IDs in list tables are **plain text**, not links.
   - Settings has a left sidebar with **Overview / Users / Security**.
   - Application info appears directly under the Overview stats.
   - User editing happens on a dedicated Edit user page, not inline.
6. Update `README.md`, this file, and the git tag when behavior changes.

## Stack

- FastAPI + Uvicorn
- Docker SDK for Python
- SQLite for users, schedules and audit log
- APScheduler for cron update jobs
- Jinja2 templates + plain CSS
- bcrypt password hashing
- Generic OIDC authorization-code flow (authentik compatible)
- Docker Compose with Traefik labels

## File map

| Path | Purpose |
| --- | --- |
| `app/main.py` | FastAPI routes, sessions, auth, container actions, schedules, settings |
| `app/dockerops.py` | Docker inventory, actions, image/network helpers, update + old-image cleanup |
| `app/scheduler.py` | APScheduler cron jobs, timezone from `TZ` |
| `app/auth.py` | Password hashing, local admin bootstrap, OIDC authorize/token/userinfo |
| `app/db.py` | SQLite schema, migrations, users, schedules, audit |
| `app/templates/` | Jinja2 UI templates |
| `app/static/style.css` | UI styling |
| `Dockerfile` | Builds the image from source (used by GitHub Actions) |
| `.github/workflows/docker-publish.yml` | Builds and publishes the image to GHCR on push/tag |
| `requirements.txt` | Python dependencies |
| `docker-compose.yml` | Generic Compose stack using the published GHCR image, volumes, networks; no Traefik labels |
| `.env` / `.env.example` | Local secrets / tracked placeholders |
| `README.md` | User-facing setup and feature docs |
| `AGENT.md` | This handover guide |

## Deployment facts

- Traefik is optional. The base Compose file has **no** reverse-proxy labels; add them in `docker-compose.override.yml` for your own host/cert resolver.
- When Traefik is used: `websecure` entry point, TLS enabled, no plain HTTP router.
- Service port: `8000`
- Image: `ghcr.io/ivar12/docker-manager:latest`, published by GitHub Actions on push to `main` and on `v*` tags
- GitHub repo: `ivar12/Docker-Manager` (public), remote via SSH `git@github.com:ivar12/Docker-Manager.git`
- Networks:
  ```yaml
  networks:
    internal:
      internal: true
    backend:
      external: true
  ```
- The service attaches only to `backend`.
- Data volume: `dockermgr_data` mounted at `/app/data`.
- Self container name: `SELF_CONTAINER=dockermgr`; the updater never updates itself.
- Timezone: `TZ=Europe/Amsterdam` by default.

## Common commands

From `/home/ivar/docker-manager`:

```bash
# Validate
python3 -m compileall -q app
docker compose config --quiet

# Build locally only when testing image changes
docker build -t ghcr.io/ivar12/docker-manager:latest .

# Run normally: pulls the published image (requires Traefik on the external backend network)
docker network create backend   # only if missing
docker compose pull
docker compose up -d

# Stop
docker compose down

# Git history
git log --oneline --decorate --all
```

Local test override (kept outside the repo) was previously used at `/tmp/opencode/docker-compose.test.yml`:

```yaml
services:
  dockermgr:
    ports:
      - "127.0.0.1:18000:8000"
    environment:
      SESSION_HTTPS_ONLY: "false"
```

Start with:

```bash
docker compose -f docker-compose.yml -f /tmp/opencode/docker-compose.test.yml pull
docker compose -f docker-compose.yml -f /tmp/opencode/docker-compose.test.yml up -d
```

Test URL: <http://localhost:18000>  
Default test login: `admin` / `changeme` (the shipped test hash; change it for real use).

## Auth notes

- Local admin username comes from `ADMIN_USERNAME`.
- The password hash is stored base64-encoded in `ADMIN_PASSWORD_HASH_B64` so Compose does not mangle bcrypt's `$`.
- Generate a new hash with:
  ```bash
  python3 -c "import bcrypt,base64; print(base64.b64encode(bcrypt.hashpw(b'your-password', bcrypt.gensalt())).decode())"
  ```
- The `.env` admin hash is restored on every restart. Changing it through the UI is temporary.
- Users have roles `admin` or `user`. Admin role grants access to Settings.
- OIDC is disabled by default. Set `OIDC_ENABLED=true` and the authentik values in `.env`.
- OIDC redirect URI: `https://dockermgr.home.networktechlab.nl/auth/callback`.
- OIDC users are provisioned on first successful login with role `user`.

## Update behavior

- Schedules use 5-field cron: `min hour day month weekday`.
- APScheduler weekday numbering is `0=Monday ... 6=Sunday` (not standard cron numbering). Use `6` for Sunday and `0-4` for Mon-Fri.
- The New/Edit schedule forms include `_cron_helper.html`, a date/time-style builder that writes the cron field. Presets: daily, weekdays, selected weekdays, hourly, custom.
- Modes:
  - `all` — all containers except opted-out and self
  - `selected` — containers labelled `dockermgr.update=true`
- Opt-out labels: `dockermgr.update=false` or `com.centurylinklabs.watchtower.enable=false`.
- A schedule can also be restricted to explicit container names.
- Updates pull the configured image, remove and recreate the container with preserved config, restart it if it was running, and remove the superseded image if no other container uses it.
- Containers with unusual options (custom macros, complex devices, etc.) should be verified after an in-place update; Compose remains the source of truth for Compose-managed containers.

## UI conventions

- Containers / Images / Networks / Schedules list pages use a **Show** button in the Actions column.
- Show targets:
  - Container → `/container/<name>`
  - Image → `/images/<id-or-tag>`
  - Network → `/networks/<id-or-name>`
  - Schedule → `/schedules/<id>` (read-only detail with Run/Edit/Pause/Delete)
- Settings:
  - Sidebar: Overview, Users, Security.
  - Overview: stat cards, then application info table directly underneath.
  - Users: Create user button, user table with role shown, Edit and Delete actions.
  - Edit user: dedicated page for role changes and password resets.
  - Security: change own password.
- Titles and IDs in list tables stay plain text.

## Database migrations

`db.init_db()` creates tables and migrates the `users` table by adding the `role` column if missing, then forces the `.env` admin to `role='admin'`.

If adding columns, follow the same pattern: `CREATE TABLE IF NOT EXISTS` for fresh installs plus a `PRAGMA table_info` check and `ALTER TABLE` for existing installs.

## Audit log

Actions worth logging via `db.log_action(actor, action, detail)`:

- login/logout and OIDC callbacks
- container start/stop/restart/update
- schedule create/edit/toggle/delete/run
- user create/edit/delete/role/password changes
- image/network deletion attempts and results
- startup and scheduler results

## Testing checklist

Before committing, verify:

```bash
python3 -m compileall -q app
docker compose config --quiet
docker build -t ghcr.io/ivar12/docker-manager:latest .
```

With the test override running:

- `/health` returns `{"ok":true}`
- `/login` accepts `admin` / `changeme`
- `/`, `/images`, `/networks`, `/schedules`, `/settings` return 200
- Show buttons open the expected detail pages
- Settings sidebar switches sections
- Create user, Edit user, role change, password reset and delete work
- Schedule create/edit/show/run/delete work
- No errors in `docker logs dockermgr`

## Git conventions

- Branch: `main`
- Meaningful commit messages (`feat:`, `fix:`, `refactor:`, `chore:`, `deploy:`)
- Annotated version tags after validated changes
- Keep the working tree clean at handover
- Current tag history includes `v1.8.6` (generic Compose without Traefik labels, README with/without Traefik)

## Historical context

The user first received an off-the-shelf Portainer/Watchtower suggestion and rejected it because they wanted a custom-built application and Portainer had no suitable update scheduling. That decision is final. Continue developing this custom app only.
