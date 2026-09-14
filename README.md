# Plex qBittorrent Manager

Keep Plex streams smooth without permanently slowing down qBittorrent.

Plex qBittorrent Manager watches active Plex sessions and automatically adjusts
qBittorrent's alternative upload limit. Streaming gets the bandwidth it needs;
when streaming stops, qBittorrent returns to its normal speed settings.

## Features

- Automatically enables alternative speeds for remote Plex streams.
- Adjusts the upload limit using bandwidth reported by Plex.
- Supports minimum and maximum upload limits plus a safety multiplier.
- Handles play, pause, buffering, stopping, and episode changes.
- Keeps current settings through brief Plex outages and safely disables
  alternative mode if Plex remains unavailable.
- Supports multiple Plex streams without double counting session handoffs.
- Integrates with
  [Plex Discord Theater](https://github.com/MonkeyGoneWIld/plex-discord-theater)
  for viewer-aware bandwidth estimates.
- Runs as a lightweight Docker container on AMD64 and ARM64.

No Tautulli, Tracearr, browser extension, or additional Plex account is needed.
The manager uses your Plex server URL and token.

## Docker Compose setup

Create a `compose.yml` file:

```yaml
services:
  plex-qbt-manager:
    image: ghcr.io/monkeygonewild/plex-qbt-manager:1.0.1
    container_name: plex-qbt-manager
    restart: unless-stopped
    ports:
      - "5252:5252"
    environment:
      PLEX_URL: "http://plex:32400"
      PLEX_TOKEN: "replace-with-your-plex-token"
      QBITTORRENT_URL: "http://qbittorrent:8080"
      QBITTORRENT_USERNAME: "admin"
      QBITTORRENT_PASSWORD: "replace-with-your-qbittorrent-password"

      DYNAMIC_UPLOAD_ENABLED: "true"
      MAX_UPLOAD_MIB: "12"
      MIN_UPLOAD_MIB: "1"
      BANDWIDTH_MULTIPLIER: "1.2"

      POLLING_INTERVAL: "5"
      STOP_DELAY_SECONDS: "30"
      PAUSE_BUFFER_DELAY_SECONDS: "60"
      PLEX_STALE_SECONDS: "120"
      LOG_LEVEL: "INFO"
```

Use addresses that are reachable from inside the manager container. Container
names such as `plex` and `qbittorrent` work when the services share a Docker
network. Otherwise, use the server's LAN address.

Start the service:

```bash
docker compose up -d
```

Find your Plex token using
[Plex's token guide](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/).
The qBittorrent Web UI must be enabled and reachable using the configured URL and
credentials.

## How upload adjustment works

`MAX_UPLOAD_MIB` is the upload capacity available before accounting for Plex.
The manager adds the bandwidth of active remote streams, applies
`BANDWIDTH_MULTIPLIER`, and subtracts that amount from the maximum. The result is
kept between `MIN_UPLOAD_MIB` and `MAX_UPLOAD_MIB`.

For example, three streams using a combined 32 Mbps with a `1.5` multiplier
reserve 48 Mbps for Plex. The conversion between Plex's Mbps and qBittorrent's
MiB/s is handled automatically.

The manager changes the alternative upload value. Your normal qBittorrent upload
and download limits, plus the alternative download limit, remain under
qBittorrent's control.

## Configuration

All settings are Docker environment variables.

| Variable | Default | Description |
|---|---:|---|
| `PLEX_URL` | `http://plex:32400` | Plex server URL reachable from the container. |
| `PLEX_TOKEN` | Required | Plex authentication token. |
| `QBITTORRENT_URL` | `http://qbittorrent:8080` | qBittorrent Web UI URL reachable from the container. |
| `QBITTORRENT_USERNAME` | Required | qBittorrent Web UI username. |
| `QBITTORRENT_PASSWORD` | Required | qBittorrent Web UI password. |
| `DYNAMIC_UPLOAD_ENABLED` | `false` | Set to `true` to calculate the alternative upload limit automatically. When false, the manager only toggles alternative mode. |
| `MAX_UPLOAD_MIB` | `12` | Maximum upload budget in MiB/s while dynamic control is active. |
| `MIN_UPLOAD_MIB` | `1` | Lowest upload limit the manager may apply. |
| `BANDWIDTH_MULTIPLIER` | `1` | Multiplies the Plex bandwidth reservation. Use a value above 1 for extra headroom. |
| `POLLING_INTERVAL` | `5` | Seconds between Plex checks. |
| `DEBOUNCE_SECONDS` | `3` | Delay before applying a less restrictive limit. More restrictive changes apply immediately. |
| `STOP_DELAY_SECONDS` | `30` | Seconds to retain bandwidth after a stream stops or disappears. |
| `PAUSE_BUFFER_DELAY_SECONDS` | `60` | Seconds to retain bandwidth after a stream pauses. |
| `PLEX_TIMEOUT` | `10` | Timeout for a Plex request, in seconds. |
| `QBITTORRENT_TIMEOUT` | `10` | Timeout for a qBittorrent request, in seconds. |
| `DRIFT_CHECK_SECONDS` | `60` | Interval for detecting qBittorrent settings changed elsewhere. |
| `PLEX_STALE_SECONDS` | `120` | Time without a successful Plex response before alternative mode is disabled. |
| `LOG_LEVEL` | `INFO` | Use `DEBUG` for session and calculation details. |
| `HTTP_PORT` | `5252` | Manager HTTP port inside the container. |

`MAX_UPLOAD_MIB` and `MIN_UPLOAD_MIB` use MiB/s. Decimal values are supported.

## [Plex Discord Theater](https://github.com/MonkeyGoneWIld/plex-discord-theater) integration

[Plex Discord Theater](https://github.com/MonkeyGoneWIld/plex-discord-theater) is
a Discord Activity for hosting synchronized Plex watch parties. Everyone watches
inside Discord, with synchronized playback and individual audio and subtitle
choices.

Because one Theater room can serve several viewers while appearing as only one
or a few Plex sessions, this integration supplies the manager with viewer counts
and Discord playback state. Plex remains the source of bandwidth data.

Use Plex Discord Theater v1.0.3 or newer. Set the same long, random secret in
Theater's `QBT_MANAGER_API_KEY` and the manager's `THEATER_API_KEY`, then add these
variables to the manager service:

```yaml
    environment:
      THEATER_URL: "http://plex-discord-theater:3000"
      THEATER_API_KEY: "replace-with-a-shared-random-secret"
      THEATER_BANDWIDTH_FACTOR: "0.85"
      THEATER_POLL_INTERVAL_SECONDS: "5"
      THEATER_TIMEOUT_SECONDS: "3"
      THEATER_STALE_SECONDS: "30"
```

The services must be able to reach each other. For one viewer, the Plex bandwidth
is counted once. For two or more viewers, the estimate is:

```text
Plex bandwidth × viewer count × THEATER_BANDWIDTH_FACTOR
```

The global `BANDWIDTH_MULTIPLIER` is applied after all ordinary Plex and Theater
streams are added together.

| Theater variable | Default | Description |
|---|---:|---|
| `THEATER_URL` | Empty | Theater URL reachable from the manager. Empty disables the integration. |
| `THEATER_API_KEY` | Empty | Shared secret matching Theater's `QBT_MANAGER_API_KEY`. |
| `THEATER_BANDWIDTH_FACTOR` | `1` | Per-viewer estimate used when a variant has at least two viewers. |
| `THEATER_POLL_INTERVAL_SECONDS` | `5` | Minimum seconds between Theater API checks. |
| `THEATER_TIMEOUT_SECONDS` | `3` | Timeout for a Theater API request. |
| `THEATER_STALE_SECONDS` | `30` | Maximum age of Theater data before it is treated as stale. |

## Optional Plex webhook

Polling works on its own. If your Plex account supports webhooks, add this URL in
Plex under **Settings → Webhooks** for faster reaction to playback changes:

```text
http://YOUR_SERVER_IP:5252/webhook
```

## Updating and monitoring

Update the container:

```bash
docker compose pull
docker compose up -d
```

View logs:

```bash
docker compose logs -f plex-qbt-manager
```

Useful endpoints:

- `GET /health` reports whether Plex and qBittorrent are connected.
- `GET /status` shows current sessions, reserved bandwidth, and the applied limit.
- `POST /webhook` receives optional Plex webhook events.

Released images are available from the
[GitHub Container Registry package](https://github.com/MonkeyGoneWIld/plex-qbt-manager/pkgs/container/plex-qbt-manager)
as `latest`, `1.0.1`, and `1.0`.

## License

[MIT](LICENSE)
