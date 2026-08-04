# Plex-qBittorrent Speed Manager

Turns qBittorrent's **alternative speed limits** on while someone is streaming remotely from Plex, and back off once they stop. Remote streams get the bandwidth; downloads get it back when nobody's watching.

[![Build and publish container image](https://github.com/MonkeyGoneWIld/plex-qbt-manager/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/MonkeyGoneWIld/plex-qbt-manager/actions/workflows/docker-publish.yml)

## How it works

- **Remote sessions only.** Sessions Plex reports as local (`local=1`) are ignored — LAN playback pulls from the same box, so throttling downloads for it is pointless. Only remote streams count.
- **Webhooks are the fast path.** Plex fires `media.play` / `media.pause` / `media.stop` / `media.buffer` at `/webhook` and the speed flips within a second.
- **Polling is the safety net.** Every `POLLING_INTERVAL` seconds the service reads `/status/sessions` from Plex directly, so it still works if webhooks aren't configured (they need Plex Pass) or a webhook gets dropped.
- **Grace periods, not instant off.** A pause or buffer starts a `PAUSE_BUFFER_DELAY_SECONDS` timer; a stop or a session vanishing from Plex starts a `STOP_DELAY_SECONDS` timer. Resuming clears the timer. Alt speeds only drop once *every* tracked session's timer has expired, so seeking and buffering don't cause speed flapping.

## Quick start

Requires Docker, and Plex + qBittorrent (with the Web UI enabled) reachable from the container.

```bash
docker network create plex-network
```

```bash
git clone https://github.com/MonkeyGoneWIld/plex-qbt-manager.git
cd plex-qbt-manager
cp .env.example .env
```

Edit `.env` with your Plex token, qBittorrent credentials, and URLs, then:

```bash
docker compose up -d
```

Compose pulls `ghcr.io/monkeygonewild/plex-qbt-manager:latest` — no local build needed. Confirm it came up:

```bash
curl -s http://localhost:5252/health
```

### Get your Plex token

Follow [Finding an authentication token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/), or read it out of any Plex Web URL as the `X-Plex-Token=` query parameter.

### Point Plex at the webhook

Plex Web → **Settings → Network → Webhooks → Add Webhook** (requires Plex Pass):

- Plex and this container on the same Docker network: `http://plex-qbt-manager:5252/webhook`
- Anything else: `http://YOUR_SERVER_IP:5252/webhook`

Without Plex Pass, skip this — polling alone works, it just reacts within `POLLING_INTERVAL` seconds instead of instantly.

## Configuration

All configuration is environment variables. `.env.example` is the annotated template.

| Variable | Default | Description |
|---|---|---|
| `PLEX_URL` | `http://plex:32400` | Plex server URL |
| `PLEX_TOKEN` | **required** | Plex authentication token |
| `QBITTORRENT_URL` | `http://qbittorrent:8080` | qBittorrent Web UI URL |
| `QBITTORRENT_USERNAME` | **required** | qBittorrent Web UI username |
| `QBITTORRENT_PASSWORD` | **required** | qBittorrent Web UI password |
| `POLLING_INTERVAL` | `5` | Seconds between Plex polls |
| `DEBOUNCE_SECONDS` | `3` | Minimum spacing between speed toggles |
| `STOP_DELAY_SECONDS` | `30` | Grace period after a stream stops or disappears |
| `PAUSE_BUFFER_DELAY_SECONDS` | `60` | Grace period after a pause or buffer |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `HTTP_PORT` | `5252` | Port the service listens on |

The container starts up refusing to run without `PLEX_TOKEN`, `QBITTORRENT_USERNAME`, and `QBITTORRENT_PASSWORD`.

### Networking notes

`PLEX_URL` and `QBITTORRENT_URL` are resolved *from inside the container*. Use container names (`http://plex:32400`) only if those containers share the `plex-network`; otherwise use the host's LAN IP. Connect existing containers with:

```bash
docker network connect plex-network plex
```

qBittorrent's alternative speed limits themselves are configured in qBittorrent (**Tools → Options → Speed → Alternative Rate Limits**). This service only flips the toggle — it never sets the values.

## Endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/health` | GET | `200` when both Plex and qBittorrent are connected, `503` otherwise. Used by the container healthcheck. |
| `/status` | GET | Full state: tracked sessions, running timers, current alt-speed state. Always `200`. |
| `/webhook` | POST | Plex webhook receiver. Accepts Plex's multipart form payload and plain JSON. |
| `/webhook-test` | GET/POST | Logs whatever it receives, verbatim. For debugging what Plex is actually sending. |

Example `/status` response while one remote stream is paused:

```json
{
  "active_sessions_count": 1,
  "total_tracked_sessions": 1,
  "sessions_detail": {
    "142_someuser": {
      "is_active": true,
      "last_seen_playing": "2026-08-04T21:14:02.113000",
      "not_playing_elapsed": 12.4,
      "not_playing_remaining": 47.6,
      "delay_seconds": 60,
      "reason": "paused/buffered"
    }
  },
  "stop_delay_seconds": 30,
  "pause_buffer_delay_seconds": 60,
  "alt_speeds_enabled": true,
  "plex_connected": true,
  "qbt_connected": true,
  "last_state_change": "2026-08-04T21:10:48.902000",
  "uptime_seconds": 3821.7
}
```

## Updating

```bash
docker compose pull && docker compose up -d
```

Every push to `main` rebuilds `:latest` for `linux/amd64` and `linux/arm64`. Tagged releases (`v1.2.3`) also publish `:1.2.3` and `:1.2`; pin to one of those if you'd rather not track `main`.

## Portainer

See [PORTAINER_DEPLOYMENT.md](PORTAINER_DEPLOYMENT.md). Short version: because the stack references a published image rather than a build context, you can paste the compose file straight into the Portainer web editor and set the environment variables in the stack UI.

## Troubleshooting

**Check the logs first** — nearly everything shows up there.

```bash
docker compose logs -f
```

| Symptom | Likely cause |
|---|---|
| Container is `unhealthy` | `/health` is returning 503. Hit `curl localhost:5252/health` to see which of `plex_connected` / `qbt_connected` is false. |
| `Failed to connect to Plex server` | Bad `PLEX_TOKEN`, or `PLEX_URL` isn't reachable from inside the container. Test with `docker exec plex-qbt-manager python -c "import urllib.request;print(urllib.request.urlopen('http://plex:32400').status)"`. |
| `Failed to connect to qBittorrent` | Wrong credentials, or qBittorrent's Web UI has host-header validation on. Either disable "Enable Host header validation" or add the container to the whitelist. |
| Speeds never change | Confirm `alt_speeds_enabled` moves in `/status`. If it does but qBittorrent doesn't slow down, the alternative rate limits aren't set in qBittorrent itself. |
| Webhooks not arriving | Plex Pass is required for webhooks. Point Plex at `/webhook-test` and check the logs to see whether anything reaches the container at all. |
| Local playback throttles downloads | Plex is reporting the session as remote. Run with `LOG_LEVEL=DEBUG` and look for the `Session local attribute:` lines. |

Simulate events without Plex:

```bash
curl -X POST http://localhost:5252/webhook -H "Content-Type: application/json" -d '{"event":"media.play","sessionKey":"999","Account":{"title":"TestUser"},"Player":{"local":false}}'
```

```bash
curl -X POST http://localhost:5252/webhook -H "Content-Type: application/json" -d '{"event":"media.stop","sessionKey":"999","Account":{"title":"TestUser"},"Player":{"local":false}}'
```

Note the stop only starts the `STOP_DELAY_SECONDS` timer — alt speeds stay on until it expires.

## Development

```bash
pip install -r requirements.txt
```

```bash
python app.py
```

Reads the same environment variables; export them or use a `.env` loader. `test_integration.py` exercises the HTTP endpoints against a running instance:

```bash
python test_integration.py http://localhost:5252
```

There's also a `Makefile` (`make help`) and `deploy.sh` wrapping the common compose operations.

## Security

`.env` holds a Plex token and your qBittorrent password in plaintext and is gitignored — keep it that way. If it was ever committed anywhere, rotate the Plex token (Plex Web → Settings → Authorized Devices) and change the qBittorrent password.

## License

[MIT](LICENSE)
