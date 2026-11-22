# Dock.IO

**Dock.IO** is a custom-built Docker management web app with scheduled image updates, container/image/network inspection, user management, and OpenID Connect support.

> This project was written from scratch. It is intentionally **not** Portainer, Watchtower, or a repackaged off-the-shelf tool.

![Python](https://img.shields.io/badge/Python-3.12-blue?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-web%20UI-009485?logo=fastapi&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)
![Version](https://img.shields.io/badge/version-v1.0.0-brightgreen)

## What it does

- **Manage containers** — start, stop, restart, and update them from the browser.
- **Update safely** — pull a new image, recreate the container with its original config, restore its running state, and remove the superseded image when unused.
- **Schedule updates** — multiple cron schedules for all containers, selected containers, or an explicit list. New/Edit forms include a date/time-style cron helper.
- **Inspect resources** — Show pages for containers, images, networks, and schedules with configuration, state, mounts, environment, labels, and attachments.
- **Delete safely** — unused images and networks can be removed; in-use and system resources are protected.
- **Authenticate users** — local bcrypt admin plus generic OIDC login.
- **Manage users** — admin-only Settings with `admin`/`user` roles, user creation/editing, and password resets.
- **Audit actions** — logins, container actions, schedules, user changes, and update results are recorded.

## Requirements

- Docker Engine with Compose v2
- A reverse proxy is optional; the app works on a local port without one

The app image is published to `ghcr.io/ivar12/dock.io:latest` by GitHub Actions. No local build is needed.

## Run it

Common setup:

```bash
git clone https://github.com/ivar12/Dock.IO.git
cd Dock.IO

cp .env.example .env
```

Edit `.env` before starting:

- Set `SESSION_SECRET` to a random value: `python3 -c "import secrets; print(secrets.token_hex(32))"`
- Replace `ADMIN_PASSWORD_HASH_B64` with your own hash:
  ```bash
  python3 -c "import bcrypt,base64; print(base64.b64encode(bcrypt.hashpw(b'your-password', bcrypt.gensalt())).decode())"
  ```
- If you enable OIDC, set `OIDC_REDIRECT_URL` to your public `/auth/callback` URL and fill in your provider's values.

The base `docker-compose.yml` is network-agnostic: it publishes no ports and adds no reverse-proxy labels. Compose creates its own default network. Choose how you want to reach the app below.

### Without a reverse proxy

Create `docker-compose.override.yml` next to `docker-compose.yml`:

```yaml
services:
  dockio:
    ports:
      - ":8080:8000"
    environment:
      SESSION_HTTPS_ONLY: "false"
```

Start it:

```bash
docker compose up -d
```

Open `http://<host>:8080`. This publishes host port `8080` on all interfaces; add an IP before the colon (for example `192.168.1.10:8080:8000`) to restrict access. `SESSION_HTTPS_ONLY=false` is for local HTTP testing; keep it `true` when the app is served over HTTPS.

### With Traefik

Create an external network for Traefik and your apps if you do not already have one, and make sure Traefik is attached to it:

```bash
docker network create proxy
```

Create `docker-compose.override.yml`:

```yaml
services:
  dockio:
    networks:
      - proxy
    labels:
      - "traefik.enable=true"
      - "traefik.docker.network=proxy"
      - "traefik.http.routers.dockio.rule=Host(`dockio.example.com`)"
      - "traefik.http.routers.dockio.entrypoints=websecure"
      - "traefik.http.routers.dockio.tls=true"
      - "traefik.http.routers.dockio.tls.certresolver=myresolver"
      - "traefik.http.services.dockio.loadbalancer.server.port=8000"

networks:
  proxy:
    external: true
```

Replace the host, network name, entry point and cert resolver with values from your Traefik setup, then start:

```bash
docker compose up -d
```

Open `https://dockio.example.com`. For another reverse proxy, either publish a local port as above or attach the container to your proxy's network.

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
- **Roles** — `admin` can access Settings; `user` cannot. The account in `ADMIN_USERNAME` is always an admin.

For setup details, deployment notes, configuration reference, and maintainer conventions, see [`AGENT.md`](AGENT.md).
