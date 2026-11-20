# Docker Manager

![Python](https://img.shields.io/badge/Python-3.12-blue?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-web%20UI-009485?logo=fastapi&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)
![Version](https://img.shields.io/badge/version-v1.8.3-brightgreen)

A **custom-built** Docker management web application with scheduled image updates, container/image/network inspection, user management, and OpenID Connect support.

> This project was written from scratch in this repository. It is intentionally **not** Portainer, Watchtower, or a repackaged off-the-shelf tool.

---

## Features

- **Container management** — list, start, stop, restart, and update containers from the web UI.
- **Safe in-place updates** — pull the configured image, recreate the container with its original config, restore running state, and remove the superseded image when unused.
- **Flexible scheduling** — multiple 5-field cron schedules for all containers, selected containers, or an explicit container list, with a date/time-style cron helper on New/Edit forms.
- **Resource browser** — Show pages for containers, images, and networks with configuration, state, mounts, environment, labels, subnets, and attachments.
- **Image & network management** — inspect and delete unused images/networks with safety checks.
- **Authentication** — local bcrypt admin plus generic OIDC authorization-code flow, tested against authentik-style endpoints.
- **User roles & settings** — admin-only Settings area with Overview, Users, and Security sections, `admin`/`user` roles, user creation/editing, and password resets.
- **Traefik ready** — websecure-only router, `production` cert resolver, no plain HTTP router.
- **Audit log** — records logins, container actions, schedules, user changes, and update results.

---

## Quick start

### Requirements

- Docker Engine with Compose v2
- An external Docker network named `backend`
- Traefik attached to `backend` with a cert resolver named `production`

### 1. Prepare secrets

```bash
cp .env.example .env
```

Then edit `.env`:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Use that value for `SESSION_SECRET`.

Generate a bcrypt password hash for the local admin:

```bash
python3 -c "import bcrypt,base64; print(base64.b64encode(bcrypt.hashpw(b'your-password', bcrypt.gensalt())).decode())"
```

Use the output for `ADMIN_PASSWORD_HASH_B64`. Base64 is used so Compose does not mangle bcrypt's `$` characters.

### 2. Create the external network

```bash
docker network create backend   # skip if it already exists
```

### 3. Build and start

```bash
cd /home/ivar/docker-manager
docker compose up -d --build
```

### 4. Open the app

<https://dockermgr.home.networktechlab.nl>

---

## Configuration

All configuration and secrets live in `.env`. The tracked [`.env.example`](.env.example) contains every supported key.

| Variable | Purpose | Default / notes |
| --- | --- | --- |
| `SESSION_SECRET` | Signs the session cookie | Generate a fresh random value |
| `SESSION_HTTPS_ONLY` | Restrict session cookie to HTTPS | `true` in production |
| `ADMIN_USERNAME` | Local break-glass admin username | `admin` |
| `ADMIN_PASSWORD_HASH_B64` | Base64-encoded bcrypt hash for the admin | Restore from `.env` on every restart |
| `OIDC_ENABLED` | Enable OpenID login | `false` |
| `OIDC_CLIENT_ID` | OIDC client ID | — |
| `OIDC_CLIENT_SECRET` | OIDC client secret | Keep secret |
| `OIDC_BASE_URL` | Authentik base URL | e.g. `https://authentik.home.networktechlab.nl` |
| `OIDC_SLUG` | Authentik application slug | `dockermgr` |
| `OIDC_AUTHORIZE_URL` | Explicit authorize endpoint | Optional override |
| `OIDC_TOKEN_URL` | Explicit token endpoint | Optional override |
| `OIDC_USERINFO_URL` | Explicit userinfo endpoint | Optional override |
| `OIDC_REDIRECT_URL` | Callback URL | `https://dockermgr.home.networktechlab.nl/auth/callback` |
| `OIDC_SCOPES` | Requested OIDC scopes | `openid profile email` |
| `OIDC_USER_CLAIM` | Claim used as username | `preferred_username` |
| `TZ` | Scheduler timezone | `Europe/Amsterdam` |
| `SELF_CONTAINER` | Container the updater must skip | `dockermgr` |

---

## Networking & Traefik

`docker-compose.yml` declares:

```yaml
networks:
  internal:
    internal: true
  backend:
    external: true
```

The service attaches only to `backend`. `internal` is defined but intentionally unused.

Traefik labels provide:

- Host rule: `dockermgr.home.networktechlab.nl`
- Entry point: `websecure` only
- TLS: enabled
- Cert resolver: `production`
- Backend port: `8000`

---

## Using the app

### Pages

| Page | What you can do |
| --- | --- |
| Containers | Start/stop/restart, manual update, open Show page |
| Images | Inspect and delete unused images |
| Networks | Inspect and delete unused networks |
| Schedules | Create, Show, Run now, Pause/Enable, Edit, Delete |
| Activity | Browse the audit log |
| Settings | Overview, user management, password change, app info |

List-page row buttons are **Show**. Titles and IDs stay plain text.

### Settings

Settings is admin-only and has a left sidebar:

- **Overview** — stats plus application/OIDC information directly underneath
- **Users** — Create user button, roles, Edit and Delete actions
- **Security** — change your own password

User editing happens on a dedicated Edit user page. Resetting a password is done by filling in the new password fields there; leaving them blank keeps the current password.

Roles:

- `admin` — full access, including Settings
- `user` — normal access to container/image/network/schedule pages

The account named by `ADMIN_USERNAME` is always an admin and is managed through `.env`.

---

## Authentication

### Local admin

The local admin always works as a break-glass login using `ADMIN_USERNAME` and the hash in `.env`.

Changing this password through the UI is temporary because `.env` is restored on restart. For a permanent change, update `ADMIN_PASSWORD_HASH_B64` in `.env` and restart:

```bash
docker compose restart
```

### OpenID Connect / authentik

Create an OAuth2/OpenID Connect provider and application in authentik yourself. The redirect URI must be:

```text
https://dockermgr.home.networktechlab.nl/auth/callback
```

Then set:

```env
OIDC_ENABLED=true
OIDC_CLIENT_ID=dockermgr
OIDC_CLIENT_SECRET=<client secret>
OIDC_BASE_URL=https://authentik.home.networktechlab.nl
OIDC_SLUG=dockermgr
OIDC_USER_CLAIM=preferred_username
OIDC_REDIRECT_URL=https://dockermgr.home.networktechlab.nl/auth/callback
```

The app derives these endpoints from `OIDC_BASE_URL` + `OIDC_SLUG`:

- `/application/o/authorize/`
- `/application/o/token/`
- `/application/o/userinfo/`

If your provider uses different URLs, set `OIDC_AUTHORIZE_URL`, `OIDC_TOKEN_URL`, and `OIDC_USERINFO_URL` explicitly.

OIDC users are provisioned on first successful login with the `user` role. An admin can promote them through Settings → Users → Edit.

---

## Update scheduling

Schedules use **5-field cron**:

```text
min hour day month weekday
```

The New/Edit schedule forms include a date/time-style cron helper with presets for daily, weekdays, selected weekdays, and hourly schedules. The raw cron field remains editable for custom schedules.

APScheduler uses `0=Monday ... 6=Sunday` for the weekday field, which differs from many standard-cron references.

Examples:

- `0 4 * * *` — every day at 04:00
- `0 4 * * 6` — Sunday at 04:00
- `0 4 * * 0-4` — Monday to Friday at 04:00

Modes:

- `all` — every container except opted-out containers and this app
- `selected` — containers labelled `dockermgr.update=true`
- Explicit container list — restrict a schedule to specific names

Label behavior:

| Label | Effect |
| --- | --- |
| `dockermgr.update=false` | Excluded from all schedules |
| `com.centurylinklabs.watchtower.enable=false` | Excluded from all schedules |
| `dockermgr.update=true` | Included in `selected` schedules |
| No label | Included in `all` schedules |

The updater never updates its own container (`SELF_CONTAINER=dockermgr`).

> Compose remains the source of truth for Compose-managed containers. In-place updates preserve the inspected runtime config, but unusual options such as custom macros or complex device mappings should be verified manually.

---

## Local testing without Traefik

For a local test on machines without Traefik, keep an override file outside the repo:

```yaml
# /tmp/opencode/docker-compose.test.yml
services:
  dockermgr:
    ports:
      - "127.0.0.1:18000:8000"
    environment:
      SESSION_HTTPS_ONLY: "false"
```

Run:

```bash
docker compose -f docker-compose.yml -f /tmp/opencode/docker-compose.test.yml up -d --build
```

Open <http://localhost:18000>.

---

## Development

```bash
python3 -m compileall -q app
docker compose config --quiet
docker compose build
```

Project structure:

```text
app/
  main.py         FastAPI routes, sessions, settings, schedules
  dockerops.py    Docker SDK inventory, actions, updates, cleanup
  scheduler.py    APScheduler cron jobs
  auth.py         Passwords, local admin, OIDC flow
  db.py           SQLite schema, migrations, users, schedules, audit
  templates/      Jinja2 UI
  static/         CSS
Dockerfile
requirements.txt
docker-compose.yml
.env.example
AGENT.md
```

See [`AGENT.md`](AGENT.md) for the full maintainer/agent handover guide.

---

## Security

`/var/run/docker.sock` gives root-equivalent access to the host. Keep this app behind Traefik + authentication and do not expose it on any other entrypoint.

- `.env` is gitignored and must never be committed.
- Secrets are never displayed in the UI.
- Use OIDC for normal users when available.
- Keep the local admin password long and store its hash in `.env`.

---

## Version history

Annotated tags are used for releases:

```bash
git log --oneline --decorate --all
git tag -n
```

Current release: **v1.8.3**.
