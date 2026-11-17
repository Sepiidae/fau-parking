"""MCP server exposing FAU parking availability over the stdio transport.

Implements the MCP JSON-RPC 2.0 protocol directly so it needs no external
``mcp`` SDK. It exposes one tool per API endpoint, backed by
:class:`fau_parking.client.ElevenXClient`.

Run (as an MCP client would):
    python -m fau_parking.mcp_server

Tools:
    fau_parking_discover_site   -> resolve the site_id for a campus
    fau_parking_zones           -> api/v2/info/zones
    fau_parking_zones_summary   -> normalized per-zone availability
    fau_parking_stalls          -> api/v2/status/stalls
    fau_parking_stalls_summary  -> normalized per-stall availability
    fau_parking_nearby          -> api/v2/info/nearby
    fau_parking_payment_providers -> api/v2/payment/providers
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

from .client import (
    ElevenXClient,
    ElevenXError,
    FAU_DEFAULT_LAT,
    FAU_DEFAULT_LNG,
    FAU_DEFAULT_SITE_ID,
)

SERVER_NAME = "fau-parking-mcp"
SERVER_VERSION = "0.1.0"

_client = ElevenXClient(site_id=FAU_DEFAULT_SITE_ID, lang="en-CA")

# Tool catalogue: name -> (description, {param: (type, required, description)})
TOOLS: Dict[str, tuple] = {
    "fau_parking_discover_site": (
        "Resolve the FAU parking site_id for a campus, optionally near a "
        "latitude/longitude.",
        {
            "lat": ("number", False, "Latitude to search around."),
            "lng": ("number", False, "Longitude to search around."),
            "radius": ("number", False, "Search radius in metres."),
        },
    ),
    "fau_parking_zones": (
        "Get zone metadata and current occupancy (api/v2/info/zones).",
        {
            "site_id": ("string", False, "Campus site id."),
            "zone_id": ("string", False, "Restrict to a zone id (repeatable)."),
        },
    ),
    "fau_parking_zones_summary": (
        "Get a normalized per-zone availability summary.",
        {
            "site_id": ("string", False, "Campus site id."),
            "zone_id": ("string", False, "Restrict to a zone id (repeatable)."),
        },
    ),
    "fau_parking_stalls": (
        "Get live per-stall status (api/v2/status/stalls).",
        {
            "site_id": ("string", False, "Campus site id."),
            "zone_id": ("string", False, "Restrict to a zone id."),
            "stall_id": ("string", False, "Restrict to a stall id."),
        },
    ),
    "fau_parking_stalls_summary": (
        "Get a normalized per-stall status summary.",
        {
            "site_id": ("string", False, "Campus site id."),
            "zone_id": ("string", False, "Restrict to a zone id."),
            "stall_id": ("string", False, "Restrict to a stall id."),
        },
    ),
    "fau_parking_nearby": (
        "Get zones near a coordinate (api/v2/info/nearby).",
        {
            "lat": ("number", False, "Latitude."),
            "lng": ("number", False, "Longitude."),
            "radius": ("number", False, "Search radius in metres."),
        },
    ),
    "fau_parking_payment_providers": (
        "List payment providers for a campus (api/v2/payment/providers).",
        {"site_id": ("string", False, "Campus site id.")},
    ),
}


def _coerce(value: Any, typ: str) -> Any:
    if typ == "number":
        return float(value)
    if typ == "integer":
        return int(value)
    if typ == "boolean":
        return bool(value)
    return str(value)


def _call_tool(name: str, args: Dict[str, Any]) -> Any:
    schema = TOOLS[name][1]
    params: Dict[str, Any] = {}
    for pname, (ptyp, _req, _desc) in schema.items():
        if pname in args and args[pname] is not None:
            params[pname] = _coerce(args[pname], ptyp)

    c = _client
    if name == "fau_parking_discover_site":
        return c.discover_site_id(
            lat=params.get("lat", FAU_DEFAULT_LAT),
            lng=params.get("lng", FAU_DEFAULT_LNG),
            radius=params.get("radius"),
        )
    if name == "fau_parking_zones":
        zone_id = args.get("zone_id")
        zone_ids = zone_id if isinstance(zone_id, list) else (
            [zone_id] if zone_id is not None else None)
        return c.info_zones(params.get("site_id"), zone_ids)
    if name == "fau_parking_zones_summary":
        data = _call_tool("fau_parking_zones", args)
        records = data if isinstance(data, list) else data.get("zones", data)
        out = []
        for z in records or []:
            if isinstance(z, dict):
                out.append({
                    "site_id": z.get("site_id") or z.get("siteId"),
                    "zone_id": z.get("zone_id") or z.get("zoneId"),
                    "name": z.get("display_name") or z.get("name"),
                    "occupied": z.get("occupied"),
                    "vacant": z.get("vacant"),
                    "total": z.get("total"),
                    "status": z.get("status"),
                })
        return out
    if name == "fau_parking_stalls":
        return c.status_stalls(
            params.get("site_id"),
            params.get("zone_id"),
            params.get("stall_id"),
        )
    if name == "fau_parking_stalls_summary":
        data = _call_tool("fau_parking_stalls", args)
        records = data if isinstance(data, list) else data.get("stalls", data)
        out = []
        for s in records or []:
            if isinstance(s, dict):
                out.append({
                    "site_id": s.get("site_id") or s.get("siteId"),
                    "zone_id": s.get("zone_id") or s.get("zoneId"),
                    "stall_id": s.get("stall_id") or s.get("stallId"),
                    "status": s.get("status") or s.get("cur_status"),
                })
        return out
    if name == "fau_parking_nearby":
        return c.info_nearby(
            lat=params.get("lat", FAU_DEFAULT_LAT),
            lng=params.get("lng", FAU_DEFAULT_LNG),
            radius=params.get("radius"),
        )
    if name == "fau_parking_payment_providers":
        return c.payment_providers(params.get("site_id"))
    raise KeyError(name)


def _tool_defs() -> List[Dict[str, Any]]:
    defs = []
    for name, (desc, schema) in TOOLS.items():
        props = {
            pname: {"type": ptyp, "description": pdesc}
            for pname, (ptyp, _req, pdesc) in schema.items()
        }
        defs.append({
            "name": name,
            "description": desc,
            "inputSchema": {
                "type": "object",
                "properties": props,
            },
        })
    return defs


def _handle_initialize(_req: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "protocolVersion": "2024-11-05",
        "capabilities": {"tools": {}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
    }


def _handle_tools_list(_req: Dict[str, Any]) -> Dict[str, Any]:
    return {"tools": _tool_defs()}


def _handle_tools_call(req: Dict[str, Any]) -> Dict[str, Any]:
    name = req.get("params", {}).get("name")
    args = req.get("params", {}).get("arguments", {}) or {}
    if name not in TOOLS:
        return {
            "content": [{"type": "text", "text": f"Unknown tool: {name}"}],
            "isError": True,
        }
    try:
        result = _call_tool(name, args)
        text = json.dumps(result, default=str)
    except ElevenXError as e:
        text = json.dumps({"error": str(e), "status": e.status, "body": e.body})
        return {"content": [{"type": "text", "text": text}], "isError": True}
    except Exception as e:  # noqa: BLE001
        return {
            "content": [{"type": "text", "text": f"Error: {e}"}],
            "isError": True,
        }
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _dispatch(req: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    method = req.get("method")
    _id = req.get("id")
    if method == "initialize":
        return {"id": _id, "result": _handle_initialize(req)}
    if method == "notifications/initialized":
        return None  # notification, no response
    if method == "tools/list":
        return {"id": _id, "result": _handle_tools_list(req)}
    if method == "tools/call":
        return {"id": _id, "result": _handle_tools_call(req)}
    if method == "ping":
        return {"id": _id, "result": {}}
    return {
        "id": _id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def serve() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            resp = {"id": None, "error": {"code": -32700, "message": str(e)}}
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
            continue
        resp = _dispatch(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


def main() -> None:
    parser = argparse.ArgumentParser(description="FAU Parking MCP server")
    parser.add_argument(
        "--site-id",
        default=os.environ.get("FAU_SITE_ID", FAU_DEFAULT_SITE_ID),
        help="Default campus site id.")
    parser.add_argument("--lang", default="en-CA", help="Language tag.")
    args = parser.parse_args()
    _client.site_id = args.site_id
    _client.lang = args.lang
    serve()


if __name__ == "__main__":
    main()
