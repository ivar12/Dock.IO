# Docker Manager

A **custom-built** Docker management web app with scheduled image updates, container/image/network inspection, user management, and OpenID Connect support.

> This project was written from scratch. It is intentionally **not** Portainer, Watchtower, or a repackaged off-the-shelf tool.

![Python](https://img.shields.io/badge/Python-3.12-blue?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-web%20UI-009485?logo=fastapi&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)
![Version](https://img.shields.io/badge/version-v1.8.6-brightgreen)

## What it does

- **Manage containers** — start, stop, restart, and update them from the browser.
- **Update safely** — pull a new image, recreate the container with its original config, restore its running state, and remove the superseded image when unused.
- **Schedule updates** — multiple cron schedules for all containers, selected containers, or an explicit list. New/Edit forms include a date/time-style cron helper.
- **Inspect resources** — Show pages for containers, images, networks, and schedules with configuration, state, mounts, environment, labels, and attachments.
- **Delete safely** — unused images and networks can be removed; in-use and system resources are protected.
- **Authenticate users** — local bcrypt admin plus generic OIDC / authentik login.
- **Manage users** — admin-only Settings with `admin`/`user` roles, user creation/editing, and password resets.
- **Audit actions** — logins, container actions, schedules, user changes, and update results are recorded.

## Requirements

- Docker Engine with Compose v2
- An external Docker network named `backend` (Traefik should also be attached to it if you use Traefik)

The app image is published to `ghcr.io/ivar12/docker-manager:latest` by GitHub Actions. No local build is needed.

## Run it

Common setup:

```bash
git clone https://github.com/ivar12/Docker-Manager.git
cd Docker-Manager

docker network create backend   # skip if it already exists
cp .env.example .env
```

Edit `.env` before starting:

- Set `SESSION_SECRET` to a random value: `python3 -c "import secrets; print(secrets.token_hex(32))"`
- Set `ADMIN_PASSWORD_HASH_B64` for your admin password (the example hash is for `changeme`):
  ```bash
  python3 -c "import bcrypt,base64; print(base64.b64encode(bcrypt.hashpw(b'your-password', bcrypt.gensalt())).decode())"
  ```

The base image exposes port `8000`. The base `docker-compose.yml` contains no reverse-proxy labels, so choose one of the two options below.

### With Traefik

Create `docker-compose.override.yml` next to `docker-compose.yml`:

```yaml
services:
  dockermgr:
    labels:
      - "traefik.enable=true"
      - "traefik.docker.network=backend"
      - "traefik.http.routers.dockermgr.rule=Host(`dockermgr.example.com`)"
      - "traefik.http.routers.dockermgr.entrypoints=websecure"
      - "traefik.http.routers.dockermgr.tls=true"
      - "traefik.http.routers.dockermgr.tls.certresolver=production"
      - "traefik.http.services.dockermgr.loadbalancer.server.port=8000"
```

Replace the host and cert resolver with your own values, then start:

```bash
docker compose up -d
```

Open `https://dockermgr.example.com`.

### Without Traefik

Create `docker-compose.override.yml` next to `docker-compose.yml` to publish a local port:

```yaml
services:
  dockermgr:
    ports:
      - "127.0.0.1:18000:8000"
    environment:
      SESSION_HTTPS_ONLY: "false"
```

Then start:

```bash
docker compose up -d
```

Open <http://localhost:18000>. `SESSION_HTTPS_ONLY=false` is only for local HTTP testing; keep it `true` when the app is behind HTTPS.

To update later:

```bash
git pull
docker compose pull
docker compose up -d
```

## Using it

- **Containers / Images / Networks / Schedules** — use the **Show** button in each row for details.
- **Schedules** — create, run now, pause, edit, show, or delete. Cron is 5-field; APScheduler weekdays are `0=Monday … 6=Sunday`.
- **Settings** — admin-only, with **Overview**, **Users**, and **Security** sections. Application info appears directly under the Overview stats.
- **Roles** — `admin` can access Settings; `user` cannot. The `.env` admin is always an admin.

For setup details, deployment notes, configuration reference, and maintainer conventions, see [`AGENT.md`](AGENT.md).
