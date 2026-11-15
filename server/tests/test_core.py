import math
import pytest
from server.pan import pans
from server.graph import neighbor_graph
from server.hush import HushRegistry

U = (37.77, -122.42)  # somewhere in SF

def ll(dx_m, dy_m, base=U):
    lat = base[0] + dy_m / 110_540.0
    lon = base[1] + dx_m / (111_320.0 * math.cos(math.radians(base[0])))
    return (lat, lon)

# ---------------- pan ----------------

def test_pans_in_range_and_extreme_hits_one():
    nbrs = [ll(-800, 10), ll(-300, -20), ll(150, 5), ll(600, -15), ll(900, 30)]
    ps, axis = pans(U, nbrs)
    assert all(-1.0 <= p <= 1.0 for p in ps)
    assert max(abs(p) for p in ps) == pytest.approx(1.0)

def test_pan_order_matches_geography_on_a_line():
    nbrs = [ll(x, 0) for x in (-500, -100, 200, 700)]
    ps, _ = pans(U, nbrs)
    assert ps == sorted(ps)

def test_sign_stability_across_frames():
    nbrs = [ll(-500, 40), ll(300, -60), ll(800, 10)]
    ps1, axis1 = pans(U, nbrs)
    jittered = [ll(-495, 45), ll(305, -55), ll(795, 12)]
    ps2, axis2 = pans(U, jittered, prev_axis=axis1)
    assert all((a < 0) == (b < 0) for a, b in zip(ps1, ps2))

def test_single_neighbor_and_degenerate():
    ps, _ = pans(U, [ll(250, 250)])
    assert abs(ps[0]) == pytest.approx(1.0)
    ps, _ = pans(U, [U, U])
    assert ps == [0.0, 0.0]
    ps, _ = pans(U, [])
    assert ps == []

def test_east_positive_default():
    ps, _ = pans(U, [ll(500, 0), ll(-500, 0)])
    assert ps[0] > 0 > ps[1]

# ---------------- graph ----------------

def test_graph_symmetric_and_mutual():
    pos = {f"u{i}": ll(i * 100, (i % 3) * 50) for i in range(10)}
    adj = neighbor_graph(pos, k=3, cap=12)
    for a, nbrs in adj.items():
        for b in nbrs:
            assert a in adj[b]

def test_reverse_neighbor_included():
    pos = {f"c{i}": ll(i * 10, 0) for i in range(8)}
    pos["loner"] = ll(50_000, 0)
    adj = neighbor_graph(pos, k=5, cap=12)
    assert len(adj["loner"]) == 5
    for b in adj["loner"]:
        assert "loner" in adj[b]

def test_degree_cap_enforced_symmetrically():
    pos = {"center": U}
    for i in range(30):
        ang = 2 * math.pi * i / 30
        pos[f"r{i}"] = ll(2000 * math.cos(ang), 2000 * math.sin(ang))
    adj = neighbor_graph(pos, k=5, cap=12)
    assert len(adj["center"]) <= 12
    for a, nbrs in adj.items():
        assert len(nbrs) <= 12
        for b in nbrs:
            assert a in adj[b]

def test_graph_deterministic():
    pos = {f"u{i}": ll((i * 37) % 900, (i * 53) % 700) for i in range(20)}
    assert neighbor_graph(pos) == neighbor_graph(dict(reversed(list(pos.items()))))

# ---------------- hush ----------------

def test_hush_ttl_and_toggle():
    t = [0.0]
    reg = HushRegistry(now=lambda: t[0], ttl_s=3600)
    assert reg.hush("a", "b") == 1
    assert reg.hush("c", "b") == 2
    assert reg.hush("a", "b") == 2
    assert reg.unhush("a", "b") == 1
    t[0] = 3601
    assert reg.count("b") == 0
    assert reg.purge() == {"b"}

def test_hushes_survive_setter_departure_by_default():
    t = [0.0]
    reg = HushRegistry(now=lambda: t[0])
    reg.hush("a", "b")
    assert reg.count("b") == 1
    assert reg.drop_user("a") == {"b"}
    assert reg.count("b") == 0

# ---------------- ice & bot ----------------

import asyncio
from unittest.mock import patch, MagicMock
from server.app import ice_servers
from sim.bot import Bot

def test_ice_servers_endpoint():
    res = asyncio.run(ice_servers())
    assert "iceServers" in res
    assert res["iceServers"] == [{"urls": ["stun:stun.l.google.com:19302", "stun:stun1.l.google.com:19302"]}]

def test_bot_ice_resilience():
    bot = Bot.__new__(Bot)
    bot.name = "testbot"

    # None url
    res = bot._ice(None)
    assert len(res) == 1
    assert res[0].urls == ["stun:stun.l.google.com:19302"]

    # Empty response
    mock_resp = MagicMock()
    mock_resp.read.return_value = b""
    mock_resp.__enter__.return_value = mock_resp
    with patch("urllib.request.urlopen", return_value=mock_resp):
        res = bot._ice("http://example.com/ice")
        assert len(res) == 1
        assert res[0].urls == ["stun:stun.l.google.com:19302"]

    # Invalid JSON
    mock_resp.read.return_value = b"<html>not json</html>"
    with patch("urllib.request.urlopen", return_value=mock_resp):
        res = bot._ice("http://example.com/ice")
        assert len(res) == 1
        assert res[0].urls == ["stun:stun.l.google.com:19302"]

    # Valid JSON
    mock_resp.read.return_value = b'{"iceServers": [{"urls": ["stun:custom.stun.com:19302"]}]}'
    with patch("urllib.request.urlopen", return_value=mock_resp):
        res = bot._ice("http://example.com/ice")
        assert len(res) == 1
        assert res[0].urls == ["stun:custom.stun.com:19302"]

from unittest.mock import AsyncMock

def test_bot_glare_handling_lower_id_ignores():
    bot = Bot.__new__(Bot)
    bot.id = "bot-a"
    bot.pcs = {}

    mock_pc = MagicMock()
    mock_pc.signalingState = "have-local-offer"
    mock_pc.setRemoteDescription = AsyncMock()
    mock_pc.createAnswer = AsyncMock()
    mock_pc.setLocalDescription = AsyncMock()
    bot.pcs["bot-b"] = mock_pc
    bot._send_desc = AsyncMock()

    # When receiving offer from bot-b (higher id) while bot-a has local offer: ignore
    asyncio.run(bot._sig("bot-b", {"description": {"type": "offer", "sdp": "v=0..."}}))
    mock_pc.setRemoteDescription.assert_not_called()
    mock_pc.createAnswer.assert_not_called()

def test_bot_glare_handling_higher_id_accepts():
    bot = Bot.__new__(Bot)
    bot.id = "bot-b"
    bot.pcs = {}

    mock_pc = MagicMock()
    mock_pc.signalingState = "stable"
    mock_pc.setRemoteDescription = AsyncMock()
    mock_pc.createAnswer = AsyncMock(return_value=MagicMock())
    mock_pc.setLocalDescription = AsyncMock()
    bot.pcs["bot-a"] = mock_pc
    bot._send_desc = AsyncMock()

    # When receiving offer from bot-a (lower id): accepts and answers
    asyncio.run(bot._sig("bot-a", {"description": {"type": "offer", "sdp": "v=0..."}}))
    mock_pc.setRemoteDescription.assert_called_once()
    mock_pc.createAnswer.assert_called_once()
    mock_pc.setLocalDescription.assert_called_once()
    bot._send_desc.assert_called_once_with("bot-a", mock_pc)


# ---------------- session takeover DoS regression tests ----------------

import hashlib
import json
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
import server.app as appmod
from server.app import app, derive_public_id


def _reset_coordinator_state():
    appmod.sessions.clear()
    appmod._session_tokens.clear()
    appmod.adjacency.clear()
    appmod.hushes = HushRegistry()


def test_derive_public_id_hides_raw_bearer_token_in_roster():
    _reset_coordinator_state()
    alice_token = "alice-secret-bearer-token-11111111"
    bob_token = "bob-secret-bearer-token-22222222"
    alice_pub = derive_public_id(alice_token)
    bob_pub = derive_public_id(bob_token)

    assert len(alice_pub) == 32
    assert alice_pub == hashlib.sha256(alice_token.encode("utf-8")).hexdigest()[:32]
    assert alice_pub != alice_token

    client = TestClient(app)
    with client.websocket_connect("/ws") as ws_alice, client.websocket_connect("/ws") as ws_bob:
        ws_alice.send_text(json.dumps({"t": "hello", "id": alice_token, "v": 0}))
        ws_alice.send_text(json.dumps({"t": "pos", "lat": 37.7700, "lon": -122.4200}))
        roster_a1 = json.loads(ws_alice.receive_text())
        assert roster_a1["t"] == "roster"

        ws_bob.send_text(json.dumps({"t": "hello", "id": bob_token, "v": 0}))
        ws_bob.send_text(json.dumps({"t": "pos", "lat": 37.7705, "lon": -122.4205}))

        # Bob receives roster containing Alice's derived public ID, never her secret token
        roster_b = json.loads(ws_bob.receive_text())
        assert roster_b["t"] == "roster"
        nbr_ids = [n["id"] for n in roster_b["neighbors"]]
        assert alice_pub in nbr_ids
        assert alice_token not in nbr_ids
        assert bob_pub in appmod.sessions
        assert alice_pub in appmod.sessions


def test_neighbor_session_takeover_dos_blocked():
    _reset_coordinator_state()
    alice_token = "alice-secret-bearer-token-aaaa1111"
    alice_pub = derive_public_id(alice_token)

    client = TestClient(app)
    with client.websocket_connect("/ws") as ws_alice:
        ws_alice.send_text(json.dumps({"t": "hello", "id": alice_token, "v": 0}))
        ws_alice.send_text(json.dumps({"t": "pos", "lat": 37.7700, "lon": -122.4200}))
        first_msg = json.loads(ws_alice.receive_text())
        assert first_msg["t"] == "roster"
        assert alice_pub in appmod.sessions

        # Attack 1: Neighbor sees alice_pub in Roster and sends Hello(id=alice_pub) to kick Alice
        with client.websocket_connect("/ws") as ws_attacker:
            ws_attacker.send_text(json.dumps({"t": "hello", "id": alice_pub, "v": 0}))
            err = json.loads(ws_attacker.receive_text())
            assert err == {"t": "error", "reason": "session_conflict"}
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws_attacker.receive_text()
            assert exc_info.value.code == 1008

        # Attack 2: Stored token digest mismatch on pub_id (simulated collision/forgery)
        orig_digest = appmod._session_tokens[alice_pub]
        appmod._session_tokens[alice_pub] = "0" * 64
        try:
            with client.websocket_connect("/ws") as ws_forger:
                ws_forger.send_text(json.dumps({"t": "hello", "id": alice_token, "v": 0}))
                err2 = json.loads(ws_forger.receive_text())
                assert err2 == {"t": "error", "reason": "session_conflict"}
                with pytest.raises(WebSocketDisconnect) as exc_info2:
                    ws_forger.receive_text()
                assert exc_info2.value.code == 1008
        finally:
            appmod._session_tokens[alice_pub] = orig_digest

        # Verify Alice's original session is still alive and responsive
        assert alice_pub in appmod.sessions
        ws_alice.send_text(json.dumps({"t": "pos", "lat": 37.7701, "lon": -122.4201}))


def test_legitimate_same_token_reconnect_and_cleanup():
    _reset_coordinator_state()
    alice_token = "alice-secret-bearer-token-reconnect"
    alice_pub = derive_public_id(alice_token)

    client = TestClient(app)
    with client.websocket_connect("/ws") as ws_old:
        ws_old.send_text(json.dumps({"t": "hello", "id": alice_token, "v": 0}))
        ws_old.send_text(json.dumps({"t": "pos", "lat": 37.7700, "lon": -122.4200}))
        assert json.loads(ws_old.receive_text())["t"] == "roster"
        old_session = appmod.sessions[alice_pub]

        # Legitimate reconnect presenting the exact same secret bearer token supersedes old socket
        with client.websocket_connect("/ws") as ws_new:
            ws_new.send_text(json.dumps({"t": "hello", "id": alice_token, "v": 0}))
            ws_new.send_text(json.dumps({"t": "pos", "lat": 37.7702, "lon": -122.4202}))
            assert json.loads(ws_new.receive_text())["t"] == "roster"
            assert appmod.sessions[alice_pub] is not old_session
            assert alice_pub in appmod._session_tokens

    # After disconnect, both sessions and _session_tokens are cleaned up
    assert alice_pub not in appmod.sessions
    assert alice_pub not in appmod._session_tokens



