"""MCP server exposing FAU parking availability, hosted over HTTP.

This is the HTTP-hosted sibling of :mod:`fau_parking.mcp_server`. It exposes the
same one-tool-per-endpoint catalogue, but is built on the **official MCP SDK**
(``mcp``) rather than re-implementing JSON-RPC over stdio by hand. Because it
uses the SDK, it can serve the tools over the two HTTP transports the MCP spec
defines:

* **SSE** (Server-Sent Events) — the classic MCP HTTP transport. A GET on the
  SSE endpoint opens an event stream; the client POSTs messages to a separate
  message endpoint.
* **Streamable HTTP** — the newer single-endpoint transport (recommended for
  new deployments).

Both are ASGI/Starlette apps, so they can be run standalone (``mcp.run(...)``)
or embedded behind any ASGI server such as uvicorn/gunicorn.

Run standalone (stdio is the default, matching the hand-rolled server):

    python -m fau_parking.mcp_http_server                 # stdio
    python -m fau_parking.mcp_http_server --transport sse --port 8000
    python -m fau_parking.mcp_http_server --transport streamable-http --port 8000

Embed behind uvicorn (Streamable HTTP at ``/mcp``):

    uvicorn fau_parking.mcp_http_server:asgi_app --port 8000

Tools (identical to the stdio server):
    fau_parking_discover_site, fau_parking_zones, fau_parking_zones_summary,
    fau_parking_stalls, fau_parking_stalls_summary, fau_parking_nearby,
    fau_parking_payment_providers
"""

from __future__ import annotations

import argparse
import inspect
import json
import logging
import os
from typing import Any, Dict, List, Optional

from .client import (
    ElevenXClient,
    ElevenXError,
    FAU_DEFAULT_LAT,
    FAU_DEFAULT_LNG,
    FAU_DEFAULT_SITE_ID,
)

logger = logging.getLogger("fau_parking.mcp_http_server")

# --------------------------------------------------------------------------- #
# SDK compatibility
# --------------------------------------------------------------------------- #
# mcp 2.x renamed ``FastMCP`` (in ``mcp.server.fastmcp``) to ``MCPServer`` (in
# ``mcp.server.mcpserver``). Import whichever is present so this module works
# across the supported SDK line.
try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as FastMCP
except ModuleNotFoundError:  # mcp < 2
    from mcp.server.fastmcp import FastMCP  # type: ignore

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

# The SDK server is created once, module-load time, so both the standalone
# runner and the embedded ASGI app expose the same instance.
mcp = FastMCP(SERVER_NAME, version=SERVER_VERSION)


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


# The SDK derives each tool's input schema from the *function signature*, so a
# tool function must expose the catalogue's param names as real parameters
# (with defaults for the optional ones). We synthesise such a signature below.
def _build_fn(name: str, schema: Dict[str, tuple]) -> Any:
    """Return a callable for ``name`` carrying the catalogue's param names.

    ``_call_tool`` accepts the raw arguments dict, so the synthesised function
    simply forwards its kwargs; the SDK validates the kwargs against the
    schema it inferred from this signature.
    """
    def _fn(**kwargs: Any) -> str:
        logger.debug("tool call %s args=%s", name, kwargs)
        try:
            result = _call_tool(name, kwargs)
            return json.dumps(result, default=str)
        except ElevenXError as e:
            text = json.dumps(
                {"error": str(e), "status": e.status, "body": e.body})
            raise RuntimeError(text) from e

    _fn.__name__ = name
    _fn.__qualname__ = name
    _fn.__doc__ = schema[name][0]

    # Build a signature: every param is a keyword-only arg; optional params
    # (required=False) get a default of None so the SDK marks them optional.
    params = []
    for pname, (ptyp, preq, pdesc) in schema.items():
        default = None if not preq else inspect.Parameter.empty
        params.append(inspect.Parameter(
            name=pname, kind=inspect.Parameter.KEYWORD_ONLY,
            default=default, annotation=Any))
    _fn.__signature__ = inspect.Signature(params)
    return _fn


def _register_tools() -> None:
    """Bind each catalogue entry to the SDK server as an MCP tool."""
    for name, (desc, schema) in TOOLS.items():
        mcp.tool(name=name, description=desc)(_build_fn(name, schema))


_register_tools()


def asgi_app(transport: str = "streamable-http",
             allowed_hosts: Optional[List[str]] = None) -> Any:
    """Return the Starlette ASGI app for the given HTTP transport.

    Use this to embed the server behind uvicorn/gunicorn, e.g.::

        uvicorn fau_parking.mcp_http_server:asgi_app --port 8000

    The SDK exposes ``streamable_http_app`` and ``sse_app`` on the server
    object in mcp >= 2; older SDKs expose them on the class/instance too.

    The SDK wraps every HTTP transport in DNS-rebinding protection that
    validates the ``Host`` header against an allow-list; with an empty
    allow-list (the default) *every* host is rejected (HTTP 421). Pass
    ``allowed_hosts`` to whitelist the Host headers you expect, or set it to
    ``[]`` to allow all (disables the check). For local/embedded use the
    default whitelists the loopback hosts a dev server normally sees.
    """
    # The SDK names the ASGI factories ``sse_app`` and ``streamable_http_app``
    # (underscore), whereas the transport is referred to as ``streamable-http``
    # (hyphen). Map explicitly rather than string-mangling the transport name.
    getter = {
        "sse": "sse_app",
        "streamable-http": "streamable_http_app",
    }.get(transport)
    factory = getattr(mcp, getter, None) if getter else None
    if factory is None:
        raise ValueError(
            f"SDK does not expose an ASGI factory for transport {transport!r}")
    kwargs: Dict[str, Any] = {}
    if allowed_hosts is not None:
        # mcp >= 2 accepts a TransportSecuritySettings object.
        try:
            from mcp.server.transport_security import TransportSecuritySettings
            kwargs["transport_security"] = TransportSecuritySettings(
                allowed_hosts=allowed_hosts)
        except ModuleNotFoundError:  # older SDK: no knob, ignore
            pass
    return factory(**kwargs)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="FAU Parking MCP server (official SDK, HTTP-hostable)")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default=os.environ.get("FAU_MCP_TRANSPORT", "stdio"),
        help="MCP transport: stdio (default), sse, or streamable-http.")
    parser.add_argument("--host", default="0.0.0.0",
                        help="Bind host for HTTP transports.")
    parser.add_argument("--port", type=int, default=8000,
                        help="Bind port for HTTP transports.")
    parser.add_argument(
        "--site-id",
        default=os.environ.get("FAU_SITE_ID", FAU_DEFAULT_SITE_ID),
        help="Default campus site id.")
    parser.add_argument("--lang", default="en-CA", help="Language tag.")
    parser.add_argument("--sse-path", default="/sse",
                        help="SSE endpoint path (SSE transport).")
    parser.add_argument("--message-path", default="/messages/",
                        help="SSE message endpoint path (SSE transport).")
    parser.add_argument("--http-path", default="/mcp",
                        help="Streamable HTTP endpoint path.")
    args = parser.parse_args()

    logging.basicConfig(
        level=os.environ.get("FAU_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s "
               "(%(filename)s:%(lineno)d)",
    )
    _client.site_id = args.site_id
    _client.lang = args.lang

    if args.transport == "stdio":
        mcp.run(transport="stdio")
        return

    if args.transport == "sse":
        mcp.run(transport="sse", host=args.host, port=args.port,
                sse_path=args.sse_path, message_path=args.message_path)
        return

    # streamable-http
    mcp.run(transport="streamable-http", host=args.host, port=args.port,
            streamable_http_path=args.http_path)


if __name__ == "__main__":
    main()
