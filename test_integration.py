#!/usr/bin/env python3
"""Integration tests for the Plex-qBittorrent Speed Manager.

Drives the HTTP endpoints of a *running* instance with synthetic Plex webhook
payloads. Does not require a real Plex or qBittorrent server — connection
warnings in the container logs are expected when running against a bare service.

Usage:
    python test_integration.py [base_url]
"""

import json
import sys
import time

import requests

DEFAULT_URL = "http://localhost:5252"


class IntegrationTester:
    def __init__(self, base_url: str = DEFAULT_URL):
        self.base_url = base_url.rstrip('/')
        self.session = requests.Session()
        self.failures = []

    # -- helpers ---------------------------------------------------------

    def check(self, condition: bool, message: str) -> bool:
        print(f"  {'PASS' if condition else 'FAIL'}  {message}")
        if not condition:
            self.failures.append(message)
        return condition

    def get_status(self) -> dict:
        response = self.session.get(f"{self.base_url}/status", timeout=10)
        return response.json() if response.status_code == 200 else {}

    def active_count(self) -> int:
        return self.get_status().get('active_sessions_count', -1)

    def send_webhook(self, event: str, session_key: str = "test123",
                     user: str = "TestUser", local: bool = False) -> bool:
        """Send a synthetic Plex webhook. `local=False` makes it a remote session."""
        payload = {
            "event": event,
            "sessionKey": session_key,
            "Account": {"title": user},
            "Metadata": {"title": "Test Movie", "type": "movie"},
            "Player": {
                "local": local,
                "state": "playing" if event in ("media.play", "media.resume") else "stopped",
            },
        }
        try:
            response = self.session.post(f"{self.base_url}/webhook", json=payload, timeout=10)
            return response.status_code == 200
        except requests.RequestException as e:
            print(f"  webhook {event} failed: {e}")
            return False

    # -- tests -----------------------------------------------------------

    def test_health_endpoint(self):
        print("\n[health] endpoint responds")
        try:
            response = self.session.get(f"{self.base_url}/health", timeout=10)
        except requests.RequestException as e:
            self.check(False, f"reachable ({e})")
            return
        # 200 = healthy, 503 = running but not connected to Plex/qBittorrent.
        self.check(response.status_code in (200, 503), f"HTTP {response.status_code}")
        body = response.json()
        self.check('status' in body, f"reports status={body.get('status')}")
        print(f"        plex_connected={body.get('plex_connected')} "
              f"qbt_connected={body.get('qbt_connected')}")

    def test_status_endpoint(self):
        print("\n[status] endpoint returns full state")
        status = self.get_status()
        for key in ('active_sessions_count', 'total_tracked_sessions', 'sessions_detail',
                    'alt_speeds_enabled', 'stop_delay_seconds', 'pause_buffer_delay_seconds'):
            self.check(key in status, f"contains '{key}'")

    def test_local_session_ignored(self):
        print("\n[local] local sessions are not tracked")
        before = self.active_count()
        self.check(self.send_webhook("media.play", "local1", "LocalUser", local=True),
                   "webhook accepted")
        time.sleep(1)
        self.check(self.active_count() == before, "active count unchanged")

    def test_play_starts_tracking(self):
        print("\n[play] remote play registers a session")
        before = self.active_count()
        self.check(self.send_webhook("media.play", "s1", "User1"), "webhook accepted")
        time.sleep(1)
        self.check(self.active_count() == before + 1, "active count incremented")

    def test_stop_uses_grace_period(self):
        print("\n[stop] stop starts a timer instead of dropping immediately")
        self.send_webhook("media.play", "s2", "User2")
        time.sleep(1)
        before = self.active_count()

        self.check(self.send_webhook("media.stop", "s2", "User2"), "stop webhook accepted")
        time.sleep(1)

        self.check(self.active_count() == before,
                   "session still active during the stop grace period")

        detail = self.get_status().get('sessions_detail', {}).get('s2_User2', {})
        self.check(detail.get('not_playing_remaining', 0) > 0,
                   f"timer running ({detail.get('not_playing_remaining')}s remaining, "
                   f"reason={detail.get('reason')})")

    def test_resume_clears_timer(self):
        print("\n[resume] resuming clears a pending timer")
        self.send_webhook("media.play", "s3", "User3")
        time.sleep(1)
        self.send_webhook("media.pause", "s3", "User3")
        time.sleep(1)

        detail = self.get_status().get('sessions_detail', {}).get('s3_User3', {})
        self.check('not_playing_remaining' in detail, "pause started a timer")

        self.send_webhook("media.play", "s3", "User3")
        time.sleep(1)

        detail = self.get_status().get('sessions_detail', {}).get('s3_User3', {})
        self.check('not_playing_remaining' not in detail, "timer cleared on resume")

    def test_multiple_sessions(self):
        print("\n[multi] concurrent sessions are counted independently")
        before = self.active_count()
        for i in range(1, 4):
            self.send_webhook("media.play", f"m{i}", f"MultiUser{i}")
            time.sleep(0.5)
        time.sleep(1)
        self.check(self.active_count() == before + 3, "all three sessions tracked")

    def test_invalid_payloads(self):
        print("\n[invalid] malformed input is rejected, not crashed on")
        response = self.session.post(f"{self.base_url}/webhook", json={}, timeout=10)
        self.check(response.status_code == 400, f"empty payload -> HTTP {response.status_code}")

        response = self.session.post(
            f"{self.base_url}/webhook",
            data="not json",
            headers={"Content-Type": "text/plain"},
            timeout=10,
        )
        self.check(response.status_code == 400, f"non-JSON body -> HTTP {response.status_code}")

        response = self.session.post(
            f"{self.base_url}/webhook",
            data='{"invalid": json}',
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        self.check(response.status_code == 400, f"malformed JSON -> HTTP {response.status_code}")

    # -- runner ----------------------------------------------------------

    def run_all_tests(self) -> bool:
        print(f"Testing {self.base_url}")
        print("=" * 60)

        self.test_health_endpoint()
        self.test_status_endpoint()
        self.test_local_session_ignored()
        self.test_play_starts_tracking()
        self.test_stop_uses_grace_period()
        self.test_resume_clears_timer()
        self.test_multiple_sessions()
        self.test_invalid_payloads()

        print("\n" + "=" * 60)
        if self.failures:
            print(f"{len(self.failures)} check(s) failed:")
            for failure in self.failures:
                print(f"  - {failure}")
        else:
            print("All checks passed.")

        print("\nFinal state:")
        print(json.dumps(self.get_status(), indent=2))
        print(
            "\nNote: sessions created by this run stay tracked until their stop timer "
            "expires (STOP_DELAY_SECONDS, default 30s), then the polling loop drops them."
        )

        return not self.failures


def main():
    base_url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    tester = IntegrationTester(base_url)
    sys.exit(0 if tester.run_all_tests() else 1)


if __name__ == "__main__":
    main()
