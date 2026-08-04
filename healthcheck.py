#!/usr/bin/env python3
"""Docker healthcheck for the Plex-qBittorrent Speed Manager.

Used as the container HEALTHCHECK so the image doesn't need curl installed.
Exits 0 when the service is healthy, 1 otherwise.
"""

import json
import os
import sys
import urllib.error
import urllib.request

TIMEOUT = 5


def fetch(url: str):
    """GET a JSON endpoint. Returns (http_status, parsed_body_or_None)."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=TIMEOUT) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        # /health answers 503 with a JSON body when degraded — that body is useful.
        try:
            return e.code, json.loads(e.read().decode())
        except (ValueError, OSError):
            return e.code, None
    except (urllib.error.URLError, OSError) as e:
        print(f"UNHEALTHY: cannot reach {url}: {e}")
        return None, None
    except json.JSONDecodeError as e:
        print(f"UNHEALTHY: invalid JSON from {url}: {e}")
        return None, None


def main():
    port = os.getenv('HTTP_PORT', '5252')
    status_code, data = fetch(f"http://127.0.0.1:{port}/health")

    if status_code is None:
        sys.exit(1)

    if data is None:
        print(f"UNHEALTHY: HTTP {status_code} with no readable body")
        sys.exit(1)

    plex = data.get('plex_connected', False)
    qbt = data.get('qbt_connected', False)
    active = data.get('active_sessions_count', 0)
    alt_speeds = data.get('alt_speeds_enabled', False)

    print(
        f"status={data.get('status', 'unknown')} plex={plex} qbt={qbt} "
        f"active_sessions={active} alt_speeds={alt_speeds}"
    )

    if status_code == 200 and plex and qbt:
        sys.exit(0)

    print("UNHEALTHY: missing connectivity to Plex and/or qBittorrent")
    sys.exit(1)


if __name__ == "__main__":
    main()
