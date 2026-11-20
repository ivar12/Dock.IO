# Docker Manager (custom-built)

This is a **custom web application**, not a repackaged Portainer/Watchtower. The Python source is in `app/` and the image is built from the local `Dockerfile`.

## What it does

- **Container management** – list all containers, start / stop / restart, and pull-new-image + recreate on demand.
- **Resource details** – use the Edit button in each row to inspect a container, image or network configuration, state, mounts, env, labels, subnets and attachments.
- **Schedule editing** – use a schedule's Edit button to change its name, cron, mode, enabled state and container selection.
- **Scheduled updates** – multiple cron schedules, either for **all** containers or only **containers you choose**.
- **Authentication** – local break-glass admin (bcrypt) **and** configurable OpenID Connect authorization-code flow (authentik works out of the box).
- **Traefik** – websecure-only router for `dockermgr.home.networktechlab.nl`, `certresolver=production`.
- **Audit log** of logins, actions, schedules and update results.

## Files

| File | Purpose |
| --- | --- |
| `app/main.py` | FastAPI routes, sessions and auth endpoints |
| `app/dockerops.py` | Docker SDK inventory, actions and in-place image updates |
| `app/scheduler.py` | APScheduler cron jobs |
| `app/auth.py` | Password hashing + OIDC authorize/token/userinfo flow |
| `app/db.py` | SQLite users, schedules and audit log |
| `app/templates/`, `app/static/` | UI |
| `Dockerfile`, `requirements.txt` | Builds the app image |
| `docker-compose.yml`, `.env` | Deployment and secrets |

## Setup

1. Ensure the external network exists and Traefik is attached to it:

   ```bash
   docker network create backend   # skip if it already exists
   ```

2. Create your local secrets file (it is deliberately gitignored):

   ```bash
   cp .env.example .env
   ```

   Then edit `.env`:
   - Replace `SESSION_SECRET` with a fresh random value.
   - Replace `ADMIN_PASSWORD_HASH_B64` (the included value is the hash for `changeme`). Generate a new one with:
     ```bash
     python3 -c "import bcrypt,base64; print(base64.b64encode(bcrypt.hashpw(b'your-password', bcrypt.gensalt())).decode())"
     ```
     Base64 is used so bcrypt's `$` characters are not mangled by Compose.
   - Set `OIDC_ENABLED=true` and the authentik values when ready.

3. Build and start:

   ```bash
   cd /home/ivar/docker-manager
   docker compose up -d --build
   ```

4. Open <https://dockermgr.home.networktechlab.nl>.

## Networks

- `backend` – `external: true`, Traefik network. The app and its scheduler use this to pull images.
- `internal` – `internal: true`, defined but not attached to any service.

## Authentication

### Local admin

User `admin` (or `ADMIN_USERNAME`) with the password whose bcrypt hash is in `.env`. This account always works as a break-glass login.

### OpenID Connect / authentik

Create an OAuth2/OpenID Connect provider + application in authentik yourself. Redirect URI must be:

```
https://dockermgr.home.networktechlab.nl/auth/callback
```

Then set in `.env`:

```
OIDC_ENABLED=true
OIDC_CLIENT_ID=<client id>
OIDC_CLIENT_SECRET=<client secret>
OIDC_BASE_URL=https://authentik.home.networktechlab.nl
OIDC_SLUG=dockermgr
OIDC_USER_CLAIM=preferred_username
OIDC_REDIRECT_URL=https://dockermgr.home.networktechlab.nl/auth/callback
```

The app derives these endpoints from `OIDC_BASE_URL` + `OIDC_SLUG`:

- `https://authentik.home.networktechlab.nl/application/o/authorize/`
- `https://authentik.home.networktechlab.nl/application/o/token/`
- `https://authentik.home.networktechlab.nl/application/o/userinfo/`

If your authentik uses different URLs, set `OIDC_AUTHORIZE_URL`, `OIDC_TOKEN_URL` and `OIDC_USERINFO_URL` explicitly. Users are provisioned on first successful OIDC login.

## Choosing what to update

On the **Containers** page, “Update now” pulls the configured image and recreates the container with its original environment, labels, binds, ports, restart policy, healthcheck and network attachments.

Label behaviour:

| Label | Effect |
| --- | --- |
| `dockermgr.update=false` or `com.centurylinklabs.watchtower.enable=false` | Excluded from **all** schedules |
| `dockermgr.update=true` | Included in **selected** schedules |
| no label | Included in **all** schedules |

This app skips its own container (`SELF_CONTAINER=dockermgr`).

On the **Schedules** page you can create schedules with a 5-field cron (e.g. `0 4 * * 0` = Sunday 04:00, in `TZ`), choose `all` or `selected` mode, and optionally tick specific container names to restrict that schedule to exactly those containers. Schedules can be run immediately, paused or deleted.

> Note: containers created by Docker Compose keep their compose labels after an in-place update, but your compose file remains the source of truth. If a container uses unusual options (custom macros, complex device mappings, etc.), verify the recreated container.

## Security

`/var/run/docker.sock` gives root-equivalent access to the host. Keep this app behind Traefik + OIDC and do not expose it on any other entrypoint.
