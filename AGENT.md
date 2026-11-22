# AGENT.md

This file is the handover guide for AI agents and future maintainers working in this repository.

## Project identity

This is **Dock.IO**, a custom-built Docker management webapp written from scratch in this repo. It is **not** Portainer, Watchtower, or a repackaged off-the-shelf product. Do not suggest or revert to those; the user explicitly wanted a custom application.

The app manages Docker containers, pulls images, recreates containers on update, runs scheduled updates, and provides local + OIDC authentication.

Current released version: **v1.2.0**.

## Golden rules

1. Keep secrets out of git. `.env` is gitignored; `.env.example` is tracked.
2. Never commit `SESSION_SECRET`, `OIDC_CLIENT_SECRET`, tokens, or database files. Admin passwords are created in the web UI and stored as bcrypt hashes in the database.
3. `/var/run/docker.sock` is mounted into the app. Treat the app as root-equivalent on the host.
4. Keep the base Compose file environment-agnostic: no external networks, no published ports, no reverse-proxy labels. Put deployment-specific networking in `docker-compose.override.yml`.
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
- Generic OIDC authorization-code flow
- Docker Compose (reverse proxy optional, configured via override)

## File map

| Path | Purpose |
| --- | --- |
| `app/main.py` | FastAPI routes, sessions, auth, container actions, schedules, settings |
| `app/dockerops.py` | Docker inventory, actions, image/network helpers, update + old-image cleanup |
| `app/scheduler.py` | APScheduler cron jobs, timezone from `TZ` |
| `app/auth.py` | Password hashing, local admin bootstrap, OIDC authorize/token/userinfo |
| `app/db.py` | SQLite schema, migrations, users, schedules, update jobs, audit |
| `app/updates.py` | Tracked background container updates with progress phases |
| `app/version.py` | Running version and cached GitHub latest-release check |
| `app/templates/` | Jinja2 UI templates |
| `app/static/style.css` | UI styling |
| `app/static/updates.js` | Polls `/updates/status` and renders live update progress |
| `Dockerfile` | Builds the image from source (used by GitHub Actions) |
| `.github/workflows/docker-publish.yml` | Builds and publishes the image to GHCR on push/tag |
| `requirements.txt` | Python dependencies |
| `docker-compose.yml` | Environment-agnostic Compose stack using the published GHCR image and a named data volume |
| `.env` / `.env.example` | Local secrets / tracked placeholders |
| `README.md` | User-facing setup and feature docs |
| `AGENT.md` | This handover guide |

## Deployment facts

- Reverse proxy is optional. The base Compose file has no published ports, external networks or proxy labels.
- For Traefik or another proxy, add networking and labels in `docker-compose.override.yml`; see `README.md` for examples.
- Service port inside the container: `8000`.
- Image: `ghcr.io/ivar12/dock.io:latest`, published by GitHub Actions on push to `main` and on `v*` tags
- GitHub repo: `ivar12/Dock.IO` (public), remote via SSH `git@github.com:ivar12/Dock.IO.git`
- Networking: Compose's default network is used. No `backend` or `internal` network is declared.
- Data volume: `dockio_data` mounted at `/app/data`.
- Self container name: `SELF_CONTAINER=dockio`; the updater never updates itself.
- Timezone: `TZ` from `.env`, default `UTC`.

## Common commands

From the repository root:

```bash
# Validate
python3 -m compileall -q app
docker compose config --quiet

# Build locally only when testing image changes
docker build -t ghcr.io/ivar12/dock.io:latest .

# Run normally: pulls the published image
docker compose pull
docker compose up -d

# Stop
docker compose down

# Git history
git log --oneline --decorate --all
```

Example local override for HTTP access without a proxy (store it outside the repo, or use `docker-compose.override.yml` as documented in `README.md`):

```yaml
services:
  dockio:
    ports:
      - ":8080:8000"
    environment:
      SESSION_HTTPS_ONLY: "false"
```

Start with:

```bash
docker compose -f docker-compose.yml -f /tmp/opencode/docker-compose.test.yml pull
docker compose -f docker-compose.yml -f /tmp/opencode/docker-compose.test.yml up -d
```

Test URL: `http://<host>:8080`  
On a fresh database the app redirects to `/setup`; create the first admin there, then sign in with those credentials.

## Auth notes

- There is no admin account in `.env`. `ADMIN_USERNAME` / `ADMIN_PASSWORD_HASH_B64` are no longer read.
- `/setup` creates the first admin only when the users table is empty (`db.create_initial_admin`). A middleware redirects all pages there until a user exists.
- Passwords are hashed with bcrypt (`auth.hash_password`) and stored in the app database; they survive restarts.
- Users have roles `admin` or `user`. Admin role grants access to Settings.
- OIDC is disabled by default. Set `OIDC_ENABLED=true` and your provider's values in `.env`.
- `OIDC_REDIRECT_URL` has no default; set it to the public `/auth/callback` URL of your deployment.
- Default endpoint paths are authentik-style (`/application/o/...`); set `OIDC_AUTHORIZE_URL`, `OIDC_TOKEN_URL` and `OIDC_USERINFO_URL` for other providers.
- OIDC users are provisioned on first successful login with role `user`.

## Update behavior

- Schedules use 5-field cron: `min hour day month weekday`.
- APScheduler weekday numbering is `0=Monday ... 6=Sunday` (not standard cron numbering). Use `6` for Sunday and `0-4` for Mon-Fri.
- The New/Edit schedule forms include `_cron_helper.html`, a date/time-style builder that writes the cron field. Presets: daily, weekdays, selected weekdays, hourly, custom.
- Modes:
  - `all` — all containers except opted-out and self
  - `selected` — containers labelled `dockio.update=true`
- Opt-out labels: `dockio.update=false` or `com.centurylinklabs.watchtower.enable=false`.
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
- Every list page (containers, images, networks, schedules, activity) has a live search box and optional dropdown filters. Implementation: `_filters.html` renders the controls, `static/filters.js` toggles elements marked `data-filter-item` inside a `data-filter-scope` container, using `data-search` and arbitrary `data-<filter-key>` attributes.

## Database migrations

`db.init_db()` creates tables and migrates the `users` table by adding the `role` column if missing. It no longer creates or modifies accounts from environment variables.

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
docker build -t ghcr.io/ivar12/dock.io:latest .
```

With the test override running:

- `/health` returns `{"ok":true}`
- On a fresh volume `/` redirects to `/setup`, and creating the first admin signs you in
- `/login` accepts the account created in the web UI
- `/`, `/images`, `/networks`, `/updates`, `/schedules`, `/settings` return 200
- The topbar shows `v<version>` beside the Dock.IO brand; `/updates` shows current/latest version and `/updates/status` returns JSON
- Container updates create tracked jobs and the Updates page auto-refreshes their phase/progress
- Search boxes and dropdown filters hide/show rows correctly on all list pages
- Show buttons open the expected detail pages
- Settings sidebar switches sections
- Create user, Edit user, role change, password reset and delete work
- Schedule create/edit/show/run/delete work
- No errors in `docker logs dockio`

## Git conventions

- Branch: `main`
- Meaningful commit messages (`feat:`, `fix:`, `refactor:`, `chore:`, `deploy:`)
- Annotated version tags after validated changes
- Keep the working tree clean at handover
- Tag history: `v1.0.0` (first public release), `v1.1.0` (search/filters + web UI first admin), `v1.2.0` (update progress + release/version display)

## Historical context

The user first received an off-the-shelf Portainer/Watchtower suggestion and rejected it because they wanted a custom-built application and Portainer had no suitable update scheduling. That decision is final. Continue developing this custom app only.
