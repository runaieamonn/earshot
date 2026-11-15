"""Regression tests for zero-storage safety kill-switch, /healthz, and M4 scenario runner."""
from __future__ import annotations

import asyncio
import math
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from server.app import adjacency, app, hushes, safety, sessions
from server.graph import neighbor_graph
from sim.run_scenario import build_parser, load_bot_specs, main as scenario_main, parse_center, ring_bots


def test_safety_manual_pause_and_hush_spike_breaker(tmp_path: Path):
    pause_file = tmp_path / "earshot.pause"
    safety.reset()
    safety.pause_flag_path = pause_file
    safety.hush_spike_limit = 3
    safety.auto_pause_cooldown_s = 120

    assert safety.pause_reason() is None
    snap0 = safety.snapshot(active_sessions=2)
    assert snap0["status"] == "ok"
    assert snap0["active_sessions"] == 2

    # Manual kill-switch via touch file (stat only, zero writes by server)
    pause_file.touch()
    assert safety.is_manual_paused() is True
    assert safety.pause_reason() == "manual_pause"
    assert safety.snapshot(active_sessions=2)["status"] == "paused"
    pause_file.unlink()
    assert safety.pause_reason() is None

    # Rolling hush-spike circuit breaker
    assert safety.record_hush() is False
    assert safety.record_hush() is False
    assert safety.record_hush() is True  # hits limit=3
    assert safety.pause_reason() == "hush_spike_cooldown"
    snap1 = safety.snapshot(active_sessions=1)
    assert snap1["status"] == "paused"
    assert snap1["pause_reason"] == "hush_spike_cooldown"
    assert snap1["total_hushes_since_boot"] == 3
    assert snap1["circuit_breaker_trips"] == 1
    assert snap1["auto_pause_remaining_s"] > 0


def test_healthz_loopback_only_and_ws_killswitch(tmp_path: Path):
    sessions.clear()
    adjacency.clear()
    hushes._expiry.clear()
    safety.reset()
    pause_file = tmp_path / "earshot.pause"
    safety.pause_flag_path = pause_file
    safety.hush_spike_limit = 2
    safety.auto_pause_cooldown_s = 60

    # Non-loopback client (TestClient default host is 'testclient') must get 404
    external_client = TestClient(app)
    r_ext = external_client.get("/healthz")
    assert r_ext.status_code == 404

    # Loopback client (127.0.0.1) succeeds and gets anonymous RAM metrics
    local_client = TestClient(app, client=("127.0.0.1", 50000))
    r_loc = local_client.get("/healthz")
    assert r_loc.status_code == 200
    body = r_loc.json()
    assert body["status"] == "ok"
    assert body["pause_reason"] is None

    # Manual pause rejects new WebSocket connections with service_paused
    pause_file.touch()
    with local_client.websocket_connect("/ws") as ws_paused:
        msg = ws_paused.receive_json()
        assert msg == {"t": "error", "reason": "service_paused"}
    pause_file.unlink()

    # Connect two nearby peers and verify Hush events feed safety.record_hush()
    with local_client.websocket_connect("/ws") as ws_a, local_client.websocket_connect("/ws") as ws_b:
        ws_a.send_json({"t": "hello", "id": "peer-alpha-01", "v": 0})
        ws_b.send_json({"t": "hello", "id": "peer-bravo-02", "v": 0})
        ws_a.send_json({"t": "pos", "lat": 37.7920, "lon": -122.4128})
        ws_b.send_json({"t": "pos", "lat": 37.7921, "lon": -122.4128})

        # Drain rosters
        _ = ws_a.receive_json()
        _ = ws_b.receive_json()
        _ = ws_a.receive_json()

        ws_a.send_json({"t": "hush", "target": "peer-bravo-02"})
        hc1 = ws_a.receive_json()
        assert hc1["t"] == "hushcount" and hc1["n"] == 1
        _ = ws_b.receive_json()

        health1 = local_client.get("/healthz").json()
        assert health1["hush_events_60s"] == 1
        assert health1["status"] == "ok"

        # Second hush trips limit=2 -> auto-pause
        ws_b.send_json({"t": "hush", "target": "peer-alpha-01"})
        _ = ws_a.receive_json()
        _ = ws_b.receive_json()

        health2 = local_client.get("/healthz").json()
        assert health2["hush_events_60s"] == 2
        assert health2["status"] == "paused"
        assert health2["pause_reason"] == "hush_spike_cooldown"
        assert health2["circuit_breaker_trips"] == 1

    sessions.clear()
    adjacency.clear()
    hushes._expiry.clear()
    safety.reset()


def test_m4_scenario_runner_ring_and_park_six():
    center_lat, center_lon = parse_center("37.7920, -122.4128")
    assert (center_lat, center_lon) == (37.7920, -122.4128)

    bots = ring_bots(center_lat, center_lon, radius_m=150.0, count=6)
    assert len(bots) == 6
    assert [b["name"] for b in bots] == ["kestrel", "merlin", "osprey", "plover", "curlew", "harrier"]
    for b in bots:
        dy_m = (b["lat"] - center_lat) * 111_320.0
        dx_m = (b["lon"] - center_lon) * 111_320.0 * math.cos(math.radians(center_lat))
        dist = math.hypot(dx_m, dy_m)
        assert 148.0 <= dist <= 152.0

    # Verify neighbor_graph connects center listener to all 6 ring bots
    positions = {"listener": (center_lat, center_lon), **{b["name"]: (b["lat"], b["lon"]) for b in bots}}
    adj = neighbor_graph(positions)
    assert len(adj["listener"]) == 6

    # Verify loading sim/scenarios/park-six.json
    ap = build_parser()
    scenario_path = Path(__file__).resolve().parents[2] / "sim" / "scenarios" / "park-six.json"
    args = ap.parse_args(["--scenario", str(scenario_path), "--stagger-s", "0", "--duration-s", "0.01"])
    specs = load_bot_specs(args, ap)
    assert len(specs) == 6
    assert specs[0]["name"] == "kestrel"

    # Verify async runner launches and cleanly cancels all 6 bots
    created_bots = []

    class DummyBot:
        def __init__(self, url, lat, lon, name, ice_url):
            self.url = url
            self.lat = lat
            self.lon = lon
            self.name = name
            self.ice_url = ice_url
            created_bots.append(self)

        async def run(self):
            await asyncio.sleep(10)

    with patch("sim.run_scenario.Bot", DummyBot):
        rc = asyncio.run(
            scenario_main(
                [
                    "--center",
                    "37.7920,-122.4128",
                    "--radius-m",
                    "150",
                    "--count",
                    "6",
                    "--stagger-s",
                    "0",
                    "--duration-s",
                    "0.02",
                ]
            )
        )
    assert rc == 0
    assert len(created_bots) == 6
