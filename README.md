# Plex-qBittorrent Speed Manager

Current release: **v1.0.0**. Container images are published as
`ghcr.io/monkeygonewild/plex-qbt-manager:latest` and
`ghcr.io/monkeygonewild/plex-qbt-manager:1.0.0`.

Turns qBittorrent's alternative speed limits on for **remote Plex streams**. Optional dynamic upload control automatically subtracts their combined bandwidth from a user-configured maximum, with a minimum upload floor and a reduction multiplier. It uses your existing Plex URL and token; Tautulli, Tracearr, and extra credentials are not required.

LAN playback is ignored because it does not use the server's internet upload bandwidth.

## How it works

- The app reads Plex's authenticated `/status/sessions` endpoint. Each remote session's `Session/@bandwidth` is counted once; LAN playback is excluded using Plex's locality fields.
- A playing or buffering session enables alternative mode. Pausing holds its last known bandwidth for `PAUSE_BUFFER_DELAY_SECONDS`; stopping or disappearing holds it for `STOP_DELAY_SECONDS`. Every observed resume clears the old timer. A session that reappears paused receives a fresh pause timer. Buffering remains protected as long as Plex reports buffering.
- Plex webhooks wake polling immediately. A `media.play` or `media.resume` webhook can also clear a stale pause timer when its server, player, media item, and account uniquely match an API session. This covers a brief resume that polling missed. Ambiguous events only wake polling; they never reset another session's timer. Hints expire after the greater of ten seconds or two polling intervals.
- Polling works without webhooks. If Plex never reports a brief resume and no matching webhook arrives, that transition cannot be recovered. Use DEBUG logs to distinguish this from a normal grace expiry.
- Once all grace periods expire, alternative mode is disabled and qBittorrent's normal limits apply. The manager changes **only the alternative upload value** in dynamic mode. Existing normal upload and download values, and alternative download values, are preserved.
- qBittorrent changes are read back and verified. Unchanged values are not rewritten. External edits are detected every `DRIFT_CHECK_SECONDS`. Protective changes apply immediately; increases and disabling alternative mode obey the full `DEBOUNCE_SECONDS` interval.

## Dynamic upload settings

Set these in `.env` (or your stack manager's environment), then recreate the container:

```dotenv
DYNAMIC_UPLOAD_ENABLED=true
MAX_UPLOAD_MIB=12
MIN_UPLOAD_MIB=1
BANDWIDTH_MULTIPLIER=1.5
LOG_LEVEL=DEBUG
```

Settings are environment variables; there is no settings webpage. Dynamic mode defaults to `false` to preserve existing toggle-only installations. With it disabled, set the alternative speed values in qBittorrent as before.

The calculation is:

```text
deduction_bytes_per_second = sum(Plex bandwidth_kbps) × 1000 × multiplier ÷ 8
upload_bytes_per_second = clamp(maximum_MiB × 1048576 − deduction, minimum_MiB × 1048576, maximum_MiB × 1048576)
```

**1 MiB/s = 8.388608 Mbps.** MiB uses 1,048,576 bytes; Mbps uses 1,000,000 bits. The multiplier increases the deduction, not the final upload limit.

For streams of 12, 5, and 15 Mbps, maximum 12 MiB/s, and minimum 1 MiB/s:

| Multiplier | Deducted bandwidth | Calculated upload | Applied qBittorrent limit |
|---|---|---|---|
| 1× | 32 Mbps | 8.185303 MiB/s | 8.184570 MiB/s (8,582,144 B/s) |
| 1.5× | 48 Mbps | 6.277954 MiB/s | 6.277344 MiB/s (6,582,272 B/s) |

qBittorrent's API uses **bytes/s**, but persists these preferences in whole **KiB/s**. The manager rounds down to that precision, except that the minimum rounds up so the configured floor is respected. The range must contain at least one whole KiB/s value. Zero, negative, non-finite, reversed, and unrepresentable limits are rejected at startup; zero must never accidentally enable unlimited upload. The API's signed integer range limits the maximum to below 2048 MiB/s.

Plex's session bandwidth is its **reserved bandwidth**, the value used for session bandwidth reporting in Tautulli. It accounts for the playback session rather than blindly using the source file's bitrate. It is not the instantaneous traffic graph. See [PlexAPI's Session documentation](https://python-plexapi.readthedocs.io/en/latest/modules/media.html#plexapi.media.Session), [Tautulli's session reader](https://github.com/Tautulli/Tautulli/blob/master/plexpy/pmsconnect.py), and [qBittorrent's speed preference implementation](https://github.com/qbittorrent/qBittorrent/blob/release-4.6.7/src/base/bittorrent/sessionimpl.cpp#L3244).

Missing, zero, or invalid session bandwidth retains that session's last positive value. If it has no previous value, dynamic mode uses the minimum upload limit and logs the reason. A fresh positive value automatically restores calculated control. No manual stream bitrate is needed.

`MAX_UPLOAD_MIB` is the budget ceiling while dynamic alternative mode is active. When nobody is streaming, qBittorrent's existing **normal** limits apply; the manager does not set normal upload to this maximum. Alternative mode still activates qBittorrent's configured alternative download limit as usual.

## Connection failures

Brief Plex failures freeze the current qBittorrent settings, including pending writes and drift correction, even if a grace timer would otherwise expire. After `PLEX_STALE_SECONDS` without a successful poll (120 seconds by default), stale session records are cleared and **alternative speeds are disabled**. The saved alternative upload preference is left unchanged. This timeout also applies when Plex is unavailable at startup. Fresh Plex data resumes normal control automatically. `/health` reports 503 throughout the failure. The timeout is checked after each request completes, so request duration and polling cadence affect the exact time of the action.

qBittorrent failures are retried on the next refresh; three failed reconciliations discard the client and trigger reconnection. A successful read alone does not erase repeated write failures. An upload write must pass readback verification before the manager enables alternative mode. `/status` distinguishes the desired limit from the last observed/applied value, which can be stale while disconnected.

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
| `DEBOUNCE_SECONDS` | `3` | Minimum spacing before relaxing limits; protective changes apply immediately |
| `STOP_DELAY_SECONDS` | `30` | Grace period after a stream stops or disappears |
| `PAUSE_BUFFER_DELAY_SECONDS` | `60` | Grace after pause or an unknown idle state; active buffering remains protected |
| `PLEX_TIMEOUT` | `10` | Plex read timeout, seconds |
| `QBITTORRENT_TIMEOUT` | `10` | qBittorrent read timeout — raise if a busy Web UI logs read timeouts |
| `DRIFT_CHECK_SECONDS` | `60` | Interval for detecting external qBittorrent changes; new budgets and failed writes are checked sooner |
| `DYNAMIC_UPLOAD_ENABLED` | `false` | Enable automatic alternative upload budgeting |
| `MAX_UPLOAD_MIB` | `12` | Maximum dynamic upload budget in MiB/s |
| `MIN_UPLOAD_MIB` | `1` | Minimum dynamic upload in MiB/s; must be positive and <= maximum |
| `BANDWIDTH_MULTIPLIER` | `1` | Positive multiplier on the bandwidth deduction; e.g. `1.5` |
| `PLEX_STALE_SECONDS` | `120` | Time without a successful Plex poll before disabling alternative speeds |
| `LOG_LEVEL` | `INFO` | `DEBUG` adds per-poll sessions, timers, calculations, readbacks, and webhook matching |
| `HTTP_PORT` | `5252` | Listen port |

Alternative download values and normal-mode limits remain in qBittorrent (**Tools → Options → Speed**). The manager owns the alternative upload value only when dynamic mode is enabled.

## Endpoints

- `GET /health` — `200` when Plex and qBittorrent are both connected, `503` otherwise. Drives the container healthcheck.
- `GET /status` — per-session state, bandwidth and source, grace timers, total reserved Mbps, multiplier, exact calculated B/s, rounded desired B/s, last applied alternative upload B/s, desired/observed mode, API failure counts, data age, and refresh count. Session map keys are Plex session keys.
- `POST /webhook` — Plex webhook receiver.

## Troubleshooting

Start with `docker compose logs -f`.

| Symptom | Cause |
|---|---|
| Container `unhealthy` | `curl localhost:5252/health` — the body names which of `plex_connected` / `qbt_connected` is false |
| `Failed to connect to Plex` | Bad token, or `PLEX_URL` unreachable from inside the container (container names only resolve on a shared network) |
| `Failed to connect to qBittorrent` | Wrong credentials, or Web UI host-header validation is rejecting the container name — disable it or whitelist |
| Speeds never change | Check `dynamic_upload_enabled`, `desired_upload_bps`, `applied_alt_upload_bps`, and `alt_speeds_enabled` in `/status`. In toggle-only mode, set alternative limits in qBittorrent |
| Webhooks never arrive | Requires Plex Pass. Set `LOG_LEVEL=DEBUG` and watch for `Webhook ...` lines |
| `urllib3 ... ReadTimeoutError` warnings | qBittorrent's Web UI is answering slowly. Raise `QBITTORRENT_TIMEOUT`, or `POLLING_INTERVAL` to ask less often |
| LAN playback throttles downloads | `LOG_LEVEL=DEBUG` prints `Plex session=... remote=... location=...` on every poll; inspect Plex's locality settings |

Check that the webhook route accepts an event (this wakes polling; it does not create a synthetic Plex session):

```bash
curl -X POST http://localhost:5252/webhook -H "Content-Type: application/json" -d '{"event":"media.play","sessionKey":"999","Account":{"title":"Test"},"Player":{"local":false}}'
```

## Logs

Everything goes to stdout, rotated by Docker's json-file driver (10 MB × 3) — that's what `docker compose logs` and any stack UI reads.

A rotating `/app/logs/app.log` (5 MB × 5) is written inside the container as well. It's discarded whenever the container is recreated, including on every update; mount `/app/logs` to an absolute host path to keep it.

`INFO` records startup settings, connections, bandwidth changes, state transitions, grace expiry, budget changes, and verified qBittorrent writes. `WARNING`/`ERROR` record missing bandwidth, ambiguous hints, external edits, connection failures, readback mismatches, and Plex stale-timeout release.

`DEBUG` adds every Plex session's locality/state/bandwidth, retained bandwidth source, remaining timers, per-cycle calculations and duration, qBittorrent readbacks/skipped writes/debounce decisions, and webhook identity matching. A `cycle` number ties calculations and reads together. Full webhook payloads are never logged. Configured tokens/passwords and URL credentials are redacted, including in exception messages. HTTP-library chatter stays at WARNING.

For a repeated-pause problem, set `LOG_LEVEL=DEBUG`, recreate the container, then reproduce play → pause → resume → pause. Look for a `state=paused -> playing` transition or a `matched resume webhook` message, followed by a fresh `grace_seconds=60`. The logs should explain the final limit and the exact grace expiry that released it. If another script or qBittorrent's scheduler changes the mode, `changed externally` identifies that path.

```bash
docker compose logs --since=10m plex-qbt-manager
curl http://localhost:5252/status
```

## Development and tests

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

Tests cover conversion and qBittorrent rounding, configuration validation, multiple sessions, quality changes, repeated pauses and missed resumes, grace expiry, missing bandwidth, outages/recovery, retries, external drift, webhook validation, and credential redaction. Local HTTP tests exercise the real PlexAPI and qBittorrent client request formats with fake servers. CI runs the tests before building/publishing the image.

To try a source branch before publishing an image, build it locally and recreate the service without pulling `latest`:

```bash
docker build -t ghcr.io/monkeygonewild/plex-qbt-manager:latest .
docker compose up -d --pull never --force-recreate
```

## License

[MIT](LICENSE)


## Discord Theater integration

Use Plex Discord Theater v1.0.3 or newer alongside plex-qbt-manager v1.0.0 or newer.
Set `QBT_MANAGER_API_KEY` in Theater to a long random secret and set the same
secret as `THEATER_API_KEY` in the manager. The Plex token remains server-side;
no extra Plex account credentials or manual bot-account setting is needed.

Manager environment example (for both services sharing one internet connection):

```dotenv
DYNAMIC_UPLOAD_ENABLED=true
THEATER_URL=http://plex-discord-theater:3000
THEATER_API_KEY=replace-with-your-shared-secret
THEATER_BANDWIDTH_FACTOR=0.8
THEATER_POLL_INTERVAL_SECONDS=5
THEATER_TIMEOUT_SECONDS=3
THEATER_STALE_SECONDS=30
```

`THEATER_URL` must be reachable from the manager container. Use HTTPS when the
connection crosses an untrusted network. Leave it empty to disable integration.
The private read-only endpoint is `/api/integrations/qbt-manager/state`, with
Bearer authentication, no browser credentials, no caching, and no token in its
response. The manager samples it on successful Plex polling cycles, at most once
per `THEATER_POLL_INTERVAL_SECONDS`; a slower `POLLING_INTERVAL` also limits its
frequency. HTTP timeout bounds each attempt.

For each audio/subtitle variant, count unique connected users with the player
open, including the host. Browsing/voice-only users are excluded. Buffering users
still count; dead WebSockets disappear after Theater's existing ping timeout.
The estimated upload bandwidth per variant is:

- No viewers: release after `STOP_DELAY_SECONDS`, retaining the prior reservation during grace.
- One viewer: Plex bandwidth × 1 (no P2P factor).
- Two or more viewers: Plex bandwidth × viewers × `THEATER_BANDWIDTH_FACTOR`.

For three variants with 12 Mbps each and audiences of 3, 2 and 1, factor 0.8:
`12 × 3 × 0.8 + 12 × 2 × 0.8 + 12 = 60 Mbps`.
Add ordinary remote Plex streams, then apply `BANDWIDTH_MULTIPLIER` to that sum,
convert decimal Mbps to bytes/s, subtract from `MAX_UPLOAD_MIB`, clamp to min/max,
and round to qBittorrent's KiB/s precision. This is a configurable P2P estimate,
not measurement of bytes actually exchanged between viewers.

Matching uses the real Plex server identity plus exact HLS Session ID, with the
Plex transcode key as fallback. Matching happens before LAN filtering because
Theater's Plex connection can be local while its viewers are remote. Its matched
Plex row is **replaced**, not added twice. `/status` shows the Plex account ID and
name for each matched variant. Ordinary streams from the same account remain
independent; account names and movie titles are never used as matching shortcuts.

Theater playback state overrides Plex for matched streams: Plex may intentionally
keep transcoding during a Discord pause. Pauses retain their previous bandwidth
for `PAUSE_BUFFER_DELAY_SECONDS`. A state revision and time-in-state distinguish
a second pause even when the manager misses the intervening resume. Retained
ownership suppresses lingering Plex sessions after pause/stop grace expires.

A brief Theater failure uses the last snapshot until `THEATER_STALE_SECONDS`.
Missing, stale, foreign-server, or ambiguous integration data selects the minimum
upload budget while Plex remains reachable, rather than trusting incomplete
viewer counts. This differs from a **Plex outage**, which freezes settings and
then disables alternative mode as described above. Recovery is automatic.
With VPS relay enabled, each occupied variant reserves one home-upload feed;
the per-viewer P2P factor does not apply. Keep relay disabled for direct delivery.

`LOG_LEVEL=DEBUG` logs snapshot sequence/age, variant, viewer count, Plex account,
base bandwidth, applied weight, effective bandwidth, matching and pause revision.
`/status` includes integration freshness and each variant's calculation inputs.
Secrets are redacted. Released multi-architecture images are available through
the GHCR package names documented above.
