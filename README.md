# Dock.IO

**Dock.IO** is a custom-built Docker management web app with scheduled image updates, container/image/network inspection, user management, and OpenID Connect support.

> This project was written from scratch. It is intentionally **not** Portainer, Watchtower, or a repackaged off-the-shelf tool.

![Python](https://img.shields.io/badge/Python-3.12-blue?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-web%20UI-009485?logo=fastapi&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)
![Version](https://img.shields.io/badge/version-v1.2.0-brightgreen)

## What it does

- **Manage containers** — start, stop, restart, and update them from the browser.
- **Update safely** — pull a new image, verify the recreated container uses that exact image, restore its running state, and remove the superseded image when no other container needs it.
- **See update progress** — manual and scheduled updates are tracked phase by phase on the **Updates** page; finished jobs leave the live list.
- **Check image freshness** — each container shows up-to-date / update-available labels, and the container stats show how many updates are available. Use **Check image updates** to refresh the digest comparison. Docker Hub images are checked through Hub's metadata API, so checks do not consume the registry pull quota; other registries use the Docker daemon.
- **Bulk actions** — select multiple containers to start/stop/restart/update, or multiple images/networks to delete at once.
- **Light and dark mode** — switch from the header; the choice is remembered in the browser.
- **Spot new releases** — the running version appears beside the Dock.IO title, and an update-available badge links to the latest GitHub release.
- **Schedule updates** — multiple cron schedules for all containers, selected containers, or an explicit list. Schedules can be created and edited, and the New/Edit forms include a date/time-style cron helper.
- **Clear schedule status** — the container list shows `no schedule` when no enabled schedule would update a container; otherwise the label shows the name(s) of the schedules that cover it, for example `weekly`.
- **Inspect resources** — Show pages for containers, images, networks, and schedules with configuration, state, mounts, environment, labels, and attachments.
- **Find things fast** — every list page has a live search box plus status/usage filters.
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
- If you enable OIDC, set `OIDC_REDIRECT_URL` to your public `/auth/callback` URL and fill in your provider's values.

No admin password hash is needed. On the first launch the app opens a **first-run setup** page where you create the administrator account from the browser; the password is bcrypt-hashed and stored in the app database. Additional users and roles are managed under **Settings → Users**.

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
- **Bulk actions** — tick the checkboxes in Containers, Images or Networks, choose an action in the bar above the table, then apply it.
- **Updates** — watch live container update progress, see the running/latest Dock.IO version, and check GitHub for a new release.
- **Self-update** — Dock.IO can update its own container. It pulls the new image, then a short-lived helper container recreates and restarts it; the app is unavailable for a few seconds. The Docker socket must be mounted (as it is in the supplied `docker-compose.yml`). The first upgrade from an older version still needs a manual `docker compose pull && docker compose up -d`.
- **Schedules** — create, run now, pause, edit, show, or delete from the list or detail page. Cron is 5-field; APScheduler weekdays are `0=Monday … 6=Sunday`.
- **Settings** — admin-only, with **Overview**, **Users**, and **Security** sections. Application info appears directly under the Overview stats.
- **Roles** — `admin` can access Settings; `user` cannot. The first account created in the web UI is an admin.

### Container update labels

Set these as Docker labels on the containers you want to control (under `labels:` in your Compose service), then **recreate the container** — Dock.IO reads them from the container config:

```yaml
services:
  myservice:
    labels:
      - dockio.update=true
```

| Label | Effect |
| --- | --- |
| `dockio.update=true` | Includes the container in `selected`-mode schedules |
| `dockio.update=false` | Opts the container out: no schedule updates it; counted in the **opted out** stat |
| no label | Eligible for `all`-mode schedules |

Opted-out containers can still be updated manually with **Update now**. Setting an invalid value (anything other than `true`/`false`/`yes`/`no`/`1`/`0`/`on`/`off`) is reported as an issue on the container page.
