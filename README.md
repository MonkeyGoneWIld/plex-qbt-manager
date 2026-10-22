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
- Keeps current settings through Plex outages of up to two minutes and then
  disables alternative mode if Plex remains unavailable.
- Supports multiple Plex streams without double counting session handoffs.
- Integrates with
  [Plex Discord Theater](https://github.com/MonkeyGoneWIld/plex-discord-theater)
  for viewer-aware bandwidth estimates, including several Theater instances at once
  and the bandwidth Theater gives for its ffmpeg engine's streams and its copies.
- Runs as a lightweight Docker container on AMD64 and ARM64.

No Tautulli, Tracearr, browser extension, or additional Plex account is needed.
The manager uses your Plex server URL and token.

## Docker Compose setup

Create a `compose.yml` file:

```yaml
services:
  plex-qbt-manager:
    image: ghcr.io/monkeygonewild/plex-qbt-manager:1.2.0
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

### Connecting to qBittorrent

The manager can reach qBittorrent in three ways. Pick one:

- **Username and password.** Set `QBITTORRENT_USERNAME` and `QBITTORRENT_PASSWORD`.
- **qBittorrent API key** (qBittorrent 5.2 or later). Generate a key in the
  qBittorrent Web UI (it starts with `qbt_`) and set it as `QBITTORRENT_API_KEY`.
  The key replaces the username and password, which can be left out.
- **qui's client proxy.** In [qui](https://github.com/autobrr/qui), open
  **Settings → Client Proxy**, create a client API key for the qBittorrent
  instance, and copy the proxy URL qui shows, such as
  `http://qui:7476/proxy/<key>`. Set it as `QUI_PROXY_URL`. qui holds the
  qBittorrent login, so `QBITTORRENT_URL` and the qBittorrent credentials are
  not used. If something in front of qui asks for basic auth, turn it off for
  the `/proxy/` path.

```yaml
      # either
      QBITTORRENT_URL: "http://qbittorrent:8080"
      QBITTORRENT_API_KEY: "qbt_replace-with-your-api-key"
      # or
      QUI_PROXY_URL: "http://qui:7476/proxy/replace-with-your-qui-client-key"
```

The startup log says which one is in use: `qBittorrent connected via=password`,
`via=API key` or `via=qui proxy`. The keys are redacted from the logs.

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

A failure never selects `MIN_UPLOAD_MIB`. A stream whose bandwidth isn't known,
or that a Theater can't account for, counts as nothing; a stream that had a
figure keeps it through its grace period. A Theater or Plex that stops
answering keeps counting for `THEATER_STALE_SECONDS` or `PLEX_STALE_SECONDS`,
then counts as nothing.

For example, three streams using a combined 32 Mbps with a `1.5` multiplier
reserve 48 Mbps for Plex. The conversion between Plex's Mbps and qBittorrent's
MiB/s is handled automatically.

The manager changes the alternative upload value. Your normal qBittorrent upload
and download limits, plus the alternative download limit, remain under
qBittorrent's control.

qBittorrent is written from a thread of its own, so a slow qBittorrent Web UI
never delays the next Plex or Theater check. A stricter limit goes out before
anything is read from qBittorrent, and is read back after.

## Configuration

All settings are Docker environment variables.

| Variable | Default | Description |
|---|---:|---|
| `PLEX_URL` | `http://plex:32400` | Plex server URL reachable from the container. |
| `PLEX_TOKEN` | Required | Plex authentication token. |
| `QBITTORRENT_URL` | `http://qbittorrent:8080` | qBittorrent Web UI URL reachable from the container. |
| `QBITTORRENT_USERNAME` | Required | qBittorrent Web UI username. Not needed with `QBITTORRENT_API_KEY` or `QUI_PROXY_URL`. |
| `QBITTORRENT_PASSWORD` | Required | qBittorrent Web UI password. Not needed with `QBITTORRENT_API_KEY` or `QUI_PROXY_URL`. |
| `QBITTORRENT_API_KEY` | Not set | qBittorrent 5.2+ Web API key (`qbt_...`), used in place of the username and password. |
| `QUI_PROXY_URL` | Not set | qui's client proxy URL, `http://<qui>:7476/proxy/<key>`. When set, the manager goes through qui instead of `QBITTORRENT_URL`, and needs no qBittorrent credentials. |
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
| `PLEX_STALE_SECONDS` | `120` | Seconds without a successful Plex response before alternative mode is disabled. Until then, current qBittorrent settings are kept. |
| `LOG_LEVEL` | `INFO` | Use `DEBUG` for session and calculation details. |
| `HTTP_PORT` | `5252` | Manager HTTP port inside the container. |

`MAX_UPLOAD_MIB` and `MIN_UPLOAD_MIB` use MiB/s. Decimal values are supported.

## Plex Discord Theater integration

[Plex Discord Theater](https://github.com/MonkeyGoneWIld/plex-discord-theater) is
a Discord Activity for hosting synchronized Plex watch parties. Everyone watches
inside Discord, with synchronized playback and individual audio and subtitle
choices.

Because one Theater room can serve several viewers while appearing as only one
or a few Plex sessions, this integration supplies the manager with viewer counts
and Discord playback state. Plex remains the source of bandwidth data, except
for streams Theater's ffmpeg engine makes (see below).

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
| `THEATER_POLL_INTERVAL_SECONDS` | `5` | Minimum seconds between Theater API checks. A Plex playback webhook brings the next check forward, at most once a second. Use `1`, with `POLLING_INTERVAL` at `1`, to see a new Theater stream within about a second. |
| `THEATER_TIMEOUT_SECONDS` | `3` | Timeout for a Theater API request. |
| `THEATER_STALE_SECONDS` | `30` | Seconds an unreachable Theater's last data keeps counting. After that, the instance is ignored until it answers again. |
| `THEATER_URL_2`, `THEATER_API_KEY_2`, ... | Empty | Additional Theater instances. See below. |

### Theater's ffmpeg engine

With Theater's `PLAYBACK_ENGINE=ffmpeg`, Theater makes streams from the file
itself, so Plex has no bandwidth for them. Theater reports what one viewer of
each such stream downloads instead, one figure for the whole stream as Plex
gives one for its own: a copy's file average, a re-encode's target bitrate.
The manager uses that figure in place of Plex's:

```text
Theater bandwidth × viewer count × THEATER_BANDWIDTH_FACTOR
```

The single-viewer, relay, pause and stop rules are the same as for Plex
streams. Theater also shows these streams in Plex's Now Playing
(`FFMPEG_REPORT_TO_PLEX`). The manager recognizes that Plex session by its
player and does not count it a second time, including the seconds Plex keeps
listing it after the stream stops. While Theater answers, turning reporting
off does not change the budget. Once the manager has stopped counting an
unreachable Theater (`THEATER_STALE_SECONDS`), those Plex sessions count as
ordinary remote streams, as any other would.

A Theater build whose ffmpeg engine does not report bandwidth gives the manager
nothing to match those streams with, so they count as nothing while they play.
Streams played through Plex are unaffected either way.

Theater also sends `bandwidth_kbps` for its Direct Stream copies from Plex:
the file's average. Plex's own figure for a copy is how fast it hands the
segments to Theater over the LAN (121 Mbps for a 1 to 5 Mbps film, in one
case), so the manager reserves Theater's figure instead, and still claims
Plex's session.

In `GET /status`, a Theater session's `bandwidth_kbps` is its reservation after
viewer weighting, and its `bandwidth_source` is `theater_snapshot` for an ffmpeg
stream or `plex_session` for a Plex one. Its `theater` details add `engine`,
`bandwidth_kbps` (one viewer's), `bandwidth_source` (`measured` or `target`) and
`plex_client_id` (the Plex player Theater reports the stream as). These are
empty for streams played through Plex. When a room's next title plays through
Plex after an ffmpeg one, its session keeps the ffmpeg stream's figure, and
`theater_snapshot`, until Plex lists a bandwidth for the new stream.

### Multiple Theater instances

To include more than one Theater, give each extra instance a numbered URL and
key. Set each Theater's `QBT_MANAGER_API_KEY` to the manager key with the same
number:

```yaml
    environment:
      THEATER_URL: "http://theater-one:3000"
      THEATER_API_KEY: "secret-shared-with-theater-one"
      THEATER_URL_2: "http://theater-two:3000"
      THEATER_API_KEY_2: "secret-shared-with-theater-two"
```

Continue with `THEATER_URL_3` and `THEATER_API_KEY_3` for a third instance.
`THEATER_BANDWIDTH_FACTOR` and the poll, timeout and stale settings apply to
every instance.

Every instance must use the same Plex server as `PLEX_URL`. If an instance
reports a different Plex server, its streams count as nothing until that is
fixed. `GET /status` lists each instance under `theater.instances`.

### When a Theater is unreachable

If a Theater stops answering, its last reported streams keep counting toward
the upload budget for `THEATER_STALE_SECONDS`. After that, the manager ignores
that instance: its reservations are released and its Plex sessions are not
counted, as if it were not configured. It is counted again as soon as it
answers. Other instances and ordinary Plex streams are unaffected.

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
as `latest`, `1.2.0`, and `1.2`.

## License

[MIT](LICENSE)
