import asyncio
import base64
import hashlib
import hmac
import pathlib
import re
from unittest.mock import patch

import subprocess

from server.app import ice_servers

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_ice_servers_mints_valid_coturn_hmac_when_configured(monkeypatch):
    monkeypatch.setenv("EARSHOT_TURN_HOST", "turn:earshot.example.com:3478")
    monkeypatch.setenv("EARSHOT_TURN_SECRET", "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")
    monkeypatch.delenv("EARSHOT_TURN_TTL_S", raising=False)

    with patch("server.app.time.time", return_value=1_700_000_000):
        res = asyncio.run(ice_servers())

    servers = res["iceServers"]
    assert len(servers) == 2
    assert servers[0] == {"urls": ["stun:stun.l.google.com:19302", "stun:stun1.l.google.com:19302"]}

    turn = servers[1]
    assert turn["urls"] == ["turn:earshot.example.com:3478"]
    assert turn["username"] == str(1_700_000_000 + 3600)

    expected_cred = base64.b64encode(
        hmac.new(
            b"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            turn["username"].encode(),
            hashlib.sha1,
        ).digest()
    ).decode()
    assert turn["credential"] == expected_cred


def test_ice_servers_custom_ttl_and_minimum_floor(monkeypatch):
    monkeypatch.setenv("EARSHOT_TURN_HOST", "  turn:earshot.example.com:3478  ")
    monkeypatch.setenv("EARSHOT_TURN_SECRET", "  test-shared-secret  ")
    monkeypatch.setenv("EARSHOT_TURN_TTL_S", "10")  # clamped to 60s minimum

    with patch("server.app.time.time", return_value=1_700_000_000):
        res = asyncio.run(ice_servers())

    assert res["iceServers"][1]["urls"] == ["turn:earshot.example.com:3478"]
    assert res["iceServers"][1]["username"] == str(1_700_000_060)


def test_ice_servers_rejects_unreplaced_placeholder_secrets(monkeypatch):
    monkeypatch.setenv("EARSHOT_TURN_HOST", "turn:earshot.example.com:3478")
    for placeholder in (
        "",
        "   ",
        "CHANGE_ME_SHARED_SECRET",
        "__EARSHOT_TURN_SECRET__",
        "__GENERATE_WITH_OPENSSL_RAND_HEX_32__",
    ):
        monkeypatch.setenv("EARSHOT_TURN_SECRET", placeholder)
        res = asyncio.run(ice_servers())
        assert res["iceServers"] == [
            {"urls": ["stun:stun.l.google.com:19302", "stun:stun1.l.google.com:19302"]}
        ]


def test_deploy_configs_externalize_turn_secret_and_enable_environment_file():
    service = (REPO_ROOT / "deploy" / "earshot-coordinator.service").read_text(encoding="utf-8")
    coturn = (REPO_ROOT / "deploy" / "coturn.conf").read_text(encoding="utf-8")
    env_example = (REPO_ROOT / "deploy" / "earshot.env.example").read_text(encoding="utf-8")
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")

    # Coordinator unit loads /etc/earshot.env and has no hardcoded EARSHOT_TURN_SECRET
    assert "EnvironmentFile=-/etc/earshot.env" in service or "EnvironmentFile=/etc/earshot.env" in service
    for line in service.splitlines():
        stripped = line.strip()
        if not stripped.startswith("#"):
            assert "EARSHOT_TURN_SECRET=" not in stripped

    # coturn.conf uses template placeholder, never a concrete secret, and blocks private-IP relay
    assert "static-auth-secret=__EARSHOT_TURN_SECRET__" in coturn
    assert "realm=__EARSHOT_DOMAIN__" in coturn
    assert "no-tcp-relay" in coturn
    assert "denied-peer-ip=127.0.0.0-127.255.255.255" in coturn
    assert "denied-peer-ip=10.0.0.0-10.255.255.255" in coturn
    assert "denied-peer-ip=169.254.0.0-169.254.255.255" in coturn

    # Env template documents both EARSHOT_TURN_HOST (uncommented!) and placeholder secret
    assert re.search(r"^EARSHOT_TURN_HOST=turn:", env_example, re.MULTILINE)
    assert re.search(r"^EARSHOT_TURN_SECRET=__GENERATE_WITH_OPENSSL_RAND_HEX_32__$", env_example, re.MULTILINE)

    # No 32+ char hex secret committed anywhere in deploy/
    hex_secret_re = re.compile(r"(?:static-auth-secret|EARSHOT_TURN_SECRET)\s*=\s*[0-9a-fA-F]{32,}")
    for path in (REPO_ROOT / "deploy").iterdir():
        if path.is_file():
            assert not hex_secret_re.search(path.read_text(encoding="utf-8")), f"Hex secret found in {path.name}"

    assert "*.env" in gitignore
    assert "!deploy/earshot.env.example" in gitignore


def test_install_sh_syntax_and_coturn_template_rendering(tmp_path):
    install_sh = REPO_ROOT / "deploy" / "install.sh"
    subprocess.run(["bash", "-n", str(install_sh)], check=True)

    # Extract and run the embedded template-rendering helper against a temp env file
    env_file = tmp_path / "earshot.env"
    env_file.write_text(
        "EARSHOT_DOMAIN=audio.example.org\n"
        "EARSHOT_TURN_HOST=turn:audio.example.org:3478\n"
        "EARSHOT_TURN_SECRET=deadbeefcafebabe0123456789abcdefdeadbeefcafebabe0123456789abcdef\n",
        encoding="utf-8",
    )
    rendered_turn = tmp_path / "turnserver.conf"

    script_text = install_sh.read_text(encoding="utf-8")
    heredoc = re.search(r"<<'PY'\n(.*?)\nPY", script_text, re.DOTALL)
    assert heredoc is not None
    subprocess.run(
        ["python3", "-", str(env_file), str(REPO_ROOT / "deploy" / "coturn.conf"), str(rendered_turn)],
        input=heredoc.group(1),
        text=True,
        check=True,
    )

    rendered = rendered_turn.read_text(encoding="utf-8")
    assert "static-auth-secret=deadbeefcafebabe0123456789abcdefdeadbeefcafebabe0123456789abcdef" in rendered
    assert "realm=audio.example.org" in rendered
    assert "__EARSHOT_TURN_SECRET__" not in rendered
    assert "__EARSHOT_DOMAIN__" not in rendered

    # Also verify placeholder secrets are rejected by the installer renderer
    bad_env = tmp_path / "bad.env"
    bad_env.write_text((REPO_ROOT / "deploy" / "earshot.env.example").read_text(encoding="utf-8"), encoding="utf-8")
    proc = subprocess.run(
        ["python3", "-", str(bad_env), str(REPO_ROOT / "deploy" / "coturn.conf"), str(tmp_path / "bad.conf")],
        input=heredoc.group(1),
        text=True,
    )
    assert proc.returncode != 0

