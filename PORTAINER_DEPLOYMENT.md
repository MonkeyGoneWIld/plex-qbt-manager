# Portainer Deployment

The stack references a published image (`ghcr.io/monkeygonewild/plex-qbt-manager`) rather than a build context, so Portainer's web editor can deploy it directly — no repository checkout on the Docker host required.

## 1. Prepare the network

The container needs to reach Plex and qBittorrent. If they're already on a shared user-defined network, use that name everywhere below instead of `plex-network`.

```bash
docker network create plex-network
docker network connect plex-network plex
docker network connect plex-network qbittorrent
```

Verify:

```bash
docker network inspect plex-network --format '{{range .Containers}}{{.Name}} {{end}}'
```

If Plex or qBittorrent runs with `network_mode: host` (common for Plex, for DLNA discovery), it won't appear here — use the host's LAN IP in `PLEX_URL` / `QBITTORRENT_URL` instead of a container name.

## 2. Create the stack

**Stacks → Add stack → Web editor.** Name it `plex-qbt-manager` and paste:

```yaml
services:
  plex-qbt-manager:
    image: ghcr.io/monkeygonewild/plex-qbt-manager:latest
    container_name: plex-qbt-manager
    restart: unless-stopped
    ports:
      - "5252:5252"
    environment:
      PLEX_URL: ${PLEX_URL:-http://plex:32400}
      PLEX_TOKEN: ${PLEX_TOKEN}
      QBITTORRENT_URL: ${QBITTORRENT_URL:-http://qbittorrent:8080}
      QBITTORRENT_USERNAME: ${QBITTORRENT_USERNAME}
      QBITTORRENT_PASSWORD: ${QBITTORRENT_PASSWORD}
      POLLING_INTERVAL: ${POLLING_INTERVAL:-5}
      DEBOUNCE_SECONDS: ${DEBOUNCE_SECONDS:-3}
      STOP_DELAY_SECONDS: ${STOP_DELAY_SECONDS:-30}
      PAUSE_BUFFER_DELAY_SECONDS: ${PAUSE_BUFFER_DELAY_SECONDS:-60}
      LOG_LEVEL: ${LOG_LEVEL:-INFO}
      HTTP_PORT: 5252
    networks:
      - plex-network
    healthcheck:
      test: ["CMD", "python", "/app/healthcheck.py"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 20s
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"

networks:
  plex-network:
    external: true
```

> Two differences from the repo's `docker-compose.yml`: the `:?required` guards are dropped (Portainer's variable substitution doesn't handle them, and the app validates the same three variables at startup anyway), and there's no `./logs` bind mount, since a web-editor stack has no meaningful working directory. Container logs are available in Portainer's **Logs** tab regardless.

## 3. Set the environment variables

In the stack's **Environment variables** section, add:

| Name | Value |
|---|---|
| `PLEX_URL` | `http://plex:32400` (or `http://192.168.x.x:32400`) |
| `PLEX_TOKEN` | your Plex token |
| `QBITTORRENT_URL` | `http://qbittorrent:8080` (or the host IP) |
| `QBITTORRENT_USERNAME` | qBittorrent Web UI user |
| `QBITTORRENT_PASSWORD` | qBittorrent Web UI password |
| `LOG_LEVEL` | `INFO` |

The timing variables are optional — the defaults in the compose file apply if you leave them out.

Click **Deploy the stack**.

## 4. Verify

**Containers → plex-qbt-manager** should go green, then show `healthy` within about a minute (the healthcheck has a 20s start period). Open the **Logs** tab and look for:

```
Connected to Plex server: Your Server Name
Connected to qBittorrent: v5.0.4
Initial alternative speeds state: disabled
Starting polling loop (interval: 5s, stop delay: 30s, pause/buffer delay: 60s)
Starting Plex-qBittorrent Speed Manager (remote sessions only) on port 5252
```

Then check the endpoint:

```
http://YOUR_SERVER_IP:5252/status
```

## 5. Configure the Plex webhook

Plex Web → **Settings → Network → Webhooks → Add Webhook** (Plex Pass required):

- Same Docker network: `http://plex-qbt-manager:5252/webhook`
- Otherwise: `http://YOUR_SERVER_IP:5252/webhook`

Optional but recommended for a first run: point it at `/webhook-test` instead, play something, and read the container logs to confirm Plex is actually reaching the container. Then switch it back to `/webhook`.

## Updating

Portainer caches the image, so pulling matters:

**Stacks → plex-qbt-manager → Update the stack**, tick **Re-pull image and redeploy**, then **Update**.

To pin a version instead of tracking `main`, change the tag to a release, e.g. `ghcr.io/monkeygonewild/plex-qbt-manager:1.0.0`.

## Troubleshooting

**Stack fails with "network plex-network declared as external, but could not be found"** — create it first (step 1), or change the compose `networks:` block to match a network that already exists.

**Container restarts immediately** — check the logs for `PLEX_TOKEN environment variable is required`. Portainer only substitutes variables that are defined in the stack's environment section; a typo in the name leaves it empty.

**Healthcheck stays red** — hit `/health` directly. It returns a JSON body with `plex_connected` and `qbt_connected` telling you which side is failing.

**qBittorrent login fails from the container** — qBittorrent's "Enable Host header validation" (Web UI options) rejects requests addressed by container name. Either disable it or add `plex-qbt-manager` to the whitelist.

**Port 5252 already in use** — change the host side only: `"5253:5252"`. Leave `HTTP_PORT` at `5252` since it's the in-container port.

### Private image

If you leave the GHCR package private, the Docker host must authenticate before it can pull:

```bash
echo YOUR_GITHUB_PAT | docker login ghcr.io -u MonkeyGoneWIld --password-stdin
```

The PAT needs the `read:packages` scope. Making the package public (GitHub → your profile → **Packages** → `plex-qbt-manager` → **Package settings** → **Change visibility**) avoids this entirely and is the simpler option for a homelab.

## Resource limits

Optional; the service idles at a few MB.

```yaml
    deploy:
      resources:
        limits:
          cpus: "0.5"
          memory: 256M
```
