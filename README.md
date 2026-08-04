# Plex-qBittorrent Speed Manager

Turns qBittorrent's alternative speed limits on while someone is streaming **remotely** from Plex, and back off once they stop. Remote streams get the bandwidth; downloads get it back when nobody's watching.

LAN playback is ignored — it pulls from the same box, so throttling downloads for it is pointless.

## How it works

A session is tracked from the moment it starts playing until its grace timer expires. Alternative speeds are on whenever at least one session is tracked.

- Plex's session list is the single source of truth. A pause or buffer starts a `PAUSE_BUFFER_DELAY_SECONDS` timer; a stop, or a session vanishing from Plex, starts a `STOP_DELAY_SECONDS` one. Resuming clears it. Speeds only drop once every tracked session's timer has expired, so seeking and buffering don't cause flapping.
- Polling every `POLLING_INTERVAL` seconds drives all of it, so the service works without Plex Pass (webhooks need it).
- Plex webhooks (`media.play`, `pause`, `stop`, `buffer`) hit `/webhook` and trigger an immediate re-read rather than being trusted on their own — a Plex webhook payload carries no `sessionKey`, so it can't be matched against the sessions the API reports. They make it react in under a second; they're never the source of truth.
- qBittorrent is re-read every cycle, not cached. If anything else flips the toggle — the Web UI, another script — it's logged and corrected rather than silently ignored.

## Deploy

```bash
docker network create plex-network   # only if you address Plex/qBT by container name
```

```bash
git clone https://github.com/MonkeyGoneWIld/plex-qbt-manager.git
cd plex-qbt-manager && cp .env.example .env
```

Fill in `.env`, then:

```bash
docker compose up -d
```

Pulls `ghcr.io/monkeygonewild/plex-qbt-manager:latest` (amd64 + arm64). For a stack UI like Portainer or Dockhand, paste `docker-compose.yml` into the editor and set the variables in its own environment section.

Then add the webhook in Plex Web → **Settings → Network → Webhooks**: `http://YOUR_SERVER_IP:5252/webhook`. Skip this without Plex Pass; polling alone works.

Update with `docker compose pull && docker compose up -d`. Every push to `main` rebuilds `:latest`.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `PLEX_URL` | `http://plex:32400` | Plex server URL, as seen *from inside the container* |
| `PLEX_TOKEN` | **required** | [Finding your token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/) |
| `QBITTORRENT_URL` | `http://qbittorrent:8080` | qBittorrent Web UI URL |
| `QBITTORRENT_USERNAME` | **required** | Web UI username |
| `QBITTORRENT_PASSWORD` | **required** | Web UI password |
| `POLLING_INTERVAL` | `5` | Seconds between Plex polls |
| `DEBOUNCE_SECONDS` | `3` | Minimum spacing between speed toggles |
| `STOP_DELAY_SECONDS` | `30` | Grace period after a stream stops or disappears |
| `PAUSE_BUFFER_DELAY_SECONDS` | `60` | Grace period after a pause or buffer |
| `PLEX_TIMEOUT` | `10` | Plex read timeout, seconds |
| `QBITTORRENT_TIMEOUT` | `10` | qBittorrent read timeout — raise if a busy Web UI logs read timeouts |
| `LOG_LEVEL` | `INFO` | `DEBUG` logs every session's local/remote decision |
| `HTTP_PORT` | `5252` | Listen port |

The alternative speed *values* live in qBittorrent (**Tools → Options → Speed → Alternative Rate Limits**). This only flips the toggle.

## Endpoints

- `GET /health` — `200` when Plex and qBittorrent are both connected, `503` otherwise. Drives the container healthcheck.
- `GET /status` — tracked sessions, running timers, current speed state.
- `POST /webhook` — Plex webhook receiver.

## Troubleshooting

Start with `docker compose logs -f`.

| Symptom | Cause |
|---|---|
| Container `unhealthy` | `curl localhost:5252/health` — the body names which of `plex_connected` / `qbt_connected` is false |
| `Failed to connect to Plex` | Bad token, or `PLEX_URL` unreachable from inside the container (container names only resolve on a shared network) |
| `Failed to connect to qBittorrent` | Wrong credentials, or Web UI host-header validation is rejecting the container name — disable it or whitelist |
| Speeds never change | Check `alt_speeds_enabled` in `/status`. If it moves but nothing slows down, the rate limits aren't set in qBittorrent itself |
| Webhooks never arrive | Requires Plex Pass. Set `LOG_LEVEL=DEBUG` and watch for `Webhook ...` lines |
| `urllib3 ... ReadTimeoutError` warnings | qBittorrent's Web UI is answering slowly. Raise `QBITTORRENT_TIMEOUT`, or `POLLING_INTERVAL` to ask less often |
| LAN playback throttles downloads | `LOG_LEVEL=DEBUG` prints `Session <key>: remote/local, state=<state>` for every session each poll — that's Plex's own `local` flag |

Simulate an event without Plex:

```bash
curl -X POST http://localhost:5252/webhook -H "Content-Type: application/json" -d '{"event":"media.play","sessionKey":"999","Account":{"title":"Test"},"Player":{"local":false}}'
```

## Logs

Everything goes to stdout, rotated by Docker's json-file driver (10 MB × 3) — that's what `docker compose logs` and any stack UI reads.

A rotating `/app/logs/app.log` (5 MB × 5) is written inside the container as well. It's discarded whenever the container is recreated, including on every update; mount `/app/logs` to an absolute host path to keep it.

`LOG_LEVEL=DEBUG` adds the per-session local/remote decision on every poll and the full webhook payloads. `urllib3`, `requests` and `plexapi` are pinned to INFO so DEBUG stays readable — poll traffic would otherwise drown it.

## License

[MIT](LICENSE)
