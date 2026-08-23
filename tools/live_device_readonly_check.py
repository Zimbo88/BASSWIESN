#!/usr/bin/env python3
"""Read-only HTTP matrix for explicitly approved SoundTouch radios."""

import argparse
import asyncio
from datetime import UTC, datetime
import json
from pathlib import Path
import sys
from xml.etree import ElementTree as ET

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from basswiesn.app.core.masterlog import write_masterlog


ENDPOINTS = ("/info", "/sources", "/presets", "/now_playing", "/volume", "/getZone")


def _xml_root(value: str) -> ET.Element:
    return ET.fromstring(value)


def parse_device_result(ip_address: str, responses: dict[str, str], errors: dict[str, str]) -> dict:
    result: dict = {
        "ip": ip_address,
        "reachable": "/info" in responses,
        "device_id": "",
        "name": "",
        "model": "",
        "firmware": "",
        "volume": None,
        "source": "",
        "now_playing": {},
        "presets_count": 0,
        "zone_status": {},
        "errors": errors,
    }
    if info := responses.get("/info"):
        root = _xml_root(info)
        result.update(
            device_id=root.attrib.get("deviceID", ""),
            name=root.findtext("name", ""),
            model=root.findtext("type", ""),
            firmware=root.findtext(".//softwareVersion", ""),
        )
    if volume := responses.get("/volume"):
        root = _xml_root(volume)
        raw = root.findtext("actualvolume") or root.findtext("targetvolume") or root.text
        try:
            result["volume"] = int(float((raw or "").strip()))
        except ValueError:
            result["volume"] = None
    if playing := responses.get("/now_playing"):
        root = _xml_root(playing)
        item = root.find("ContentItem")
        source = root.attrib.get("source", "") or (item.attrib.get("source", "") if item is not None else "")
        result["source"] = source
        result["now_playing"] = {
            "source": source,
            "play_status": root.findtext("playStatus", ""),
            "station_name": root.findtext("stationName", "") or (item.findtext("itemName", "") if item is not None else ""),
            "track": root.findtext("track", ""),
            "artist": root.findtext("artist", ""),
        }
    if presets := responses.get("/presets"):
        result["presets_count"] = len(_xml_root(presets).findall(".//preset"))
    if zone := responses.get("/getZone"):
        root = _xml_root(zone)
        result["zone_status"] = {
            "active": bool(root.attrib or list(root)),
            "master": root.attrib.get("master", ""),
            "sender": root.attrib.get("senderIPAddress", ""),
            "members": [node.attrib.get("ipaddress", "") or (node.text or "").strip() for node in root.findall(".//member")],
        }
    return result


async def check_device(ip_address: str, timeout: float = 4.0) -> dict:
    responses: dict[str, str] = {}
    errors: dict[str, str] = {}
    async with httpx.AsyncClient(timeout=timeout) as client:
        for endpoint in ENDPOINTS:
            write_masterlog("live_http_read_start", ip_address=ip_address, endpoint=endpoint)
            try:
                response = await client.get(f"http://{ip_address}:8090{endpoint}")
                response.raise_for_status()
                responses[endpoint] = response.text
                write_masterlog("live_http_read_complete", ip_address=ip_address, endpoint=endpoint, status_code=response.status_code, bytes=len(response.content))
            except (httpx.HTTPError, OSError) as exc:
                errors[endpoint] = str(exc)
                write_masterlog("live_http_read_error", ip_address=ip_address, endpoint=endpoint, error_type=type(exc).__name__, error_reason=str(exc))
    try:
        return parse_device_result(ip_address, responses, errors)
    except ET.ParseError as exc:
        errors["xml_parse"] = str(exc)
        write_masterlog("live_http_read_error", ip_address=ip_address, endpoint="xml_parse", error_type="ParseError", error_reason=str(exc))
        return parse_device_result(ip_address, {}, errors)


async def run(ip_addresses: list[str], timeout: float) -> dict:
    return {
        "created_at": datetime.now(UTC).isoformat(),
        "mode": "read-only",
        "devices": await asyncio.gather(*(check_device(ip, timeout) for ip in ip_addresses)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ips", nargs="+", help="Explicitly approved radio IP addresses")
    parser.add_argument("--timeout", type=float, default=4.0)
    parser.add_argument("--output-dir", type=Path, default=Path("data/live-tests"))
    args = parser.parse_args()
    payload = asyncio.run(run(args.ips, args.timeout))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output = args.output_dir / f"device-readonly-{stamp}.json"
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(output)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if all(device["reachable"] for device in payload["devices"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
