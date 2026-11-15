"""Concurrent multi-bot park scenario runner for Earshot field tests.

Examples:
  # Run fixed Golden Gate Park 6-bot scenario against production:
  python3 -m sim.run_scenario --scenario sim/scenarios/park-six.json

  # Generate 6 bots in a 150 m ring around Huntington Park (by 1200 California St):
  python3 -m sim.run_scenario --center 37.7920,-122.4128 --radius-m 150 --count 6

  # Local test against uvicorn on localhost:8700:
  python3 -m sim.run_scenario --ws-url ws://127.0.0.1:8700/ws --ice-url http://127.0.0.1:8700/ice --scenario sim/scenarios/park-six.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import signal
from pathlib import Path
from typing import Sequence

from sim.bot import Bot

CALLSIGNS = [
    "kestrel", "merlin", "osprey", "plover", "curlew", "harrier",
    "linnet", "pipit", "turnstone", "godwit", "dunlin", "wigeon",
]


def ring_bots(center_lat: float, center_lon: float, radius_m: float, count: int) -> list[dict]:
    """Place `count` bots evenly on a circle of `radius_m` meters around center."""
    if count <= 0:
        raise ValueError("count must be positive")
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * math.cos(math.radians(center_lat))
    specs: list[dict] = []
    for i in range(count):
        theta = (2.0 * math.pi * i) / count
        d_north_m = radius_m * math.cos(theta)
        d_east_m = radius_m * math.sin(theta)
        name = CALLSIGNS[i] if i < len(CALLSIGNS) else f"bot-{i+1}"
        specs.append(
            {
                "name": name,
                "lat": round(center_lat + d_north_m / m_per_deg_lat, 6),
                "lon": round(center_lon + d_east_m / m_per_deg_lon, 6),
            }
        )
    return specs


def parse_center(raw: str) -> tuple[float, float]:
    lat_s, lon_s = raw.split(",", 1)
    return float(lat_s.strip()), float(lon_s.strip())


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Run concurrent Earshot park bots")
    ap.add_argument("--ws-url", default="wss://helloo.cloud/ws", help="Coordinator WebSocket URL")
    ap.add_argument("--ice-url", default="https://helloo.cloud/ice", help="ICE config URL (blank to disable)")
    ap.add_argument("--scenario", type=Path, help="Path to scenario JSON (e.g. sim/scenarios/park-six.json)")
    ap.add_argument("--center", help="LAT,LON center for generated ring (e.g. 37.7920,-122.4128)")
    ap.add_argument("--radius-m", type=float, default=150.0, help="Ring radius in meters (default: 150)")
    ap.add_argument("--count", type=int, default=6, help="Bot count for --center mode (default: 6)")
    ap.add_argument("--stagger-s", type=float, default=0.35, help="Delay between launching bots (seconds)")
    ap.add_argument("--duration-s", type=float, default=0.0, help="Stop automatically after N seconds (0=Ctrl-C)")
    return ap


def load_bot_specs(args: argparse.Namespace, ap: argparse.ArgumentParser | None = None) -> list[dict]:
    if args.scenario:
        data = json.loads(args.scenario.read_text(encoding="utf-8"))
        bot_specs = data["bots"]
        print(f"Loaded {len(bot_specs)} bots from {args.scenario} (center={data.get('center')})")
        return bot_specs
    if args.center:
        lat, lon = parse_center(args.center)
        bot_specs = ring_bots(lat, lon, args.radius_m, args.count)
        print(f"Generated {len(bot_specs)} bots on {args.radius_m:.0f} m circle around {lat:.5f},{lon:.5f}")
        return bot_specs
    if ap is not None:
        ap.error("Specify either --scenario or --center LAT,LON")
    raise ValueError("Specify either --scenario or --center LAT,LON")


async def main(argv: Sequence[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    bot_specs = load_bot_specs(args, ap)

    ice_url = args.ice_url or None
    tasks: list[asyncio.Task] = []
    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    for spec in bot_specs:
        bot = Bot(
            url=args.ws_url,
            lat=float(spec["lat"]),
            lon=float(spec["lon"]),
            name=spec["name"],
            ice_url=ice_url,
        )
        print(f"  + {spec['name']} @ ({spec['lat']:.5f}, {spec['lon']:.5f})")
        tasks.append(asyncio.create_task(bot.run(), name=f"bot-{spec['name']}"))
        await asyncio.sleep(args.stagger_s)

    if args.duration_s > 0:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=args.duration_s)
        except asyncio.TimeoutError:
            pass
    else:
        await stop_event.wait()

    print("Shutting down bots...")
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(0)
