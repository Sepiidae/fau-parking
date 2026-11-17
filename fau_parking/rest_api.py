"""FastAPI/uvicorn REST API for FAU parking availability.

Wraps :class:`fau_parking.client.ElevenXClient` behind a small JSON HTTP API.
Served by uvicorn (ASGI); the app object is a FastAPI instance exposed as
``app`` so it can be launched with ``uvicorn fau_parking.rest_api:app`` or via
``python -m fau_parking.rest_api``.

Run:
    python -m fau_parking.rest_api            # http://127.0.0.1:5000
    FAU_PORT=8080 python -m fau_parking.rest_api
    uvicorn fau_parking.rest_api:app --port 8080

Endpoints
    GET /health                          -> liveness
    GET /sites                           -> discover the site_id for a campus
    GET /zones                           -> api/v2/info/zones
    GET /zones/summary                   -> normalized per-zone availability
    GET /stalls                          -> api/v2/status/stalls
    GET /stalls/summary                  -> normalized per-stall availability
    GET /nearby                          -> api/v2/info/nearby
    GET /payment/providers               -> api/v2/payment/providers

Logging
    Verbosity is controlled by ``FAU_LOG_LEVEL`` (default ``INFO``).
    DEBUG  - resolved query params, signed request path, truncated raw
             upstream body.
    INFO   - one line per request: method, path, status, latency, client.
    WARNING- non-fatal anomalies (upstream returned a non-JSON body such as
             the "Welcome to nav hub" landing page, unexpected shapes).
    ERROR  - upstream HTTP errors and unhandled exceptions.
    CRITICAL - fatal misconfiguration.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from .client import (
    ElevenXClient,
    ElevenXError,
    FAU_DEFAULT_LAT,
    FAU_DEFAULT_LNG,
    FAU_DEFAULT_SITE_ID,
)

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
_LOG_LEVEL = os.environ.get("FAU_LOG_LEVEL", "INFO").upper()
_LOG_LEVEL_CODE = getattr(logging, _LOG_LEVEL, logging.INFO)
logging.basicConfig(
    level=_LOG_LEVEL_CODE,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s "
           "(%(filename)s:%(lineno)d)",
)
logger = logging.getLogger("fau_parking.rest_api")
# Keep uvicorn's own loggers in step with our configured level.
for _name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
    logging.getLogger(_name).setLevel(_LOG_LEVEL_CODE)


def _lenient_float(raw: Optional[str], default: float) -> float:
    """Parse a query value to float, falling back to ``default`` on bad input.

    Mirrors the previous Flask behaviour: an empty, missing or malformed
    value yields the default rather than a 422.
    """
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning("bad float for %r: %r (using default %s)",
                       raw, raw, default)
        return default


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #
app = FastAPI(
    title="FAU Parking REST API",
    description="JSON proxy over the ElevenX/ExactPark FAU parking API.",
    version="1.0.0",
)

# A single client instance is shared; site_id is resolved per-request when the
# caller does not pin one, so the shared default is only a fallback.
_client_kwargs: Dict[str, Any] = dict(
    site_id=os.environ.get("FAU_SITE_ID", FAU_DEFAULT_SITE_ID),
    lang=os.environ.get("FAU_LANG", "en-CA"),
)
# The upstream API requires a client_id (a device UUID); without it the server
# answers with an HTML landing page instead of JSON. ElevenXClient defaults to
# a freshly generated UUID, so only override when FAU_CLIENT_ID is explicitly set.
if os.environ.get("FAU_CLIENT_ID"):
    _client_kwargs["client_id"] = os.environ["FAU_CLIENT_ID"]
_client = ElevenXClient(**_client_kwargs)


def _get_client() -> ElevenXClient:
    return _client


# --------------------------------------------------------------------------- #
# Payload helpers
# --------------------------------------------------------------------------- #
def _check_json(data: Any, key: str) -> Any:
    """Raise if the upstream body was not JSON (e.g. the landing page).

    The client returns parsed JSON on success but the raw response text when
    the body is not valid JSON. A string here almost always means the request
    was missing a required param (typically ``client_id``); surface it as a
    clear 502 with a WARNING log instead of letting callers crash on ``.get``.
    """
    if isinstance(data, str):
        logger.warning("upstream %r returned non-JSON body: %.160r", key, data)
        raise ElevenXError(502, key, data)
    return data


def _as_records(data: Any, key: str) -> List[Any]:
    """Normalize an upstream payload into a list of records.

    * list  -> returned as-is
    * dict  -> ``data[key]`` if it is a list, else the single record wrapped
    * other -> treated as a non-JSON body and raised as a 502
    """
    data = _check_json(data, key)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        nested = data.get(key)
        if isinstance(nested, list):
            return nested
        return [data]
    raise ElevenXError(502, key, repr(data))


# --------------------------------------------------------------------------- #
# Request logging middleware
# --------------------------------------------------------------------------- #
@app.middleware("http")
async def log_requests(request: Request, call_next):
    client_host = request.client.host if request.client else "-"
    logger.info("request %s %s from %s",
                request.method, request.url.path, client_host)
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("request query: %s", request.url.query)
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:  # noqa: BLE001 - log then re-raise
        logger.exception("unhandled exception on %s %s",
                         request.method, request.url.path)
        raise
    elapsed_ms = (time.perf_counter() - start) * 1000
    logger.info("response %s %s -> %s (%.1fms)",
                request.method, request.url.path,
                response.status_code, elapsed_ms)
    return response


# --------------------------------------------------------------------------- #
# Upstream error handling
# --------------------------------------------------------------------------- #
@app.exception_handler(ElevenXError)
async def _elevenx_error_handler(request: Request, exc: ElevenXError):
    logger.error("upstream error on %s %s: HTTP %s | %s",
                 request.method, request.url.path, exc.status,
                 (exc.body or "")[:200])
    return JSONResponse(
        status_code=exc.status,
        content={"error": str(exc), "status": exc.status, "body": exc.body},
    )


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    return {"status": "ok", "service": "fau-parking-rest"}


# --------------------------------------------------------------------------- #
# Sites
# --------------------------------------------------------------------------- #
@app.get("/sites")
def sites(
    lat: Optional[str] = Query(None, description="Latitude"),
    lng: Optional[str] = Query(None, description="Longitude"),
    radius: Optional[str] = Query(None, description="Search radius (metres)"),
):
    lat_f = _lenient_float(lat, FAU_DEFAULT_LAT)
    lng_f = _lenient_float(lng, FAU_DEFAULT_LNG)
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("sites params: lat=%s lng=%s radius=%s", lat, lng, radius)
    try:
        site_id = _get_client().discover_site_id(
            lat=lat_f, lng=lng_f,
            radius=int(radius) if radius else None,
        )
    except ElevenXError as e:
        raise e
    logger.info("resolved site_id=%s for lat=%s lng=%s", site_id, lat_f, lng_f)
    return {"site_id": site_id, "lat": lat_f, "lng": lng_f}


# --------------------------------------------------------------------------- #
# Zones
# --------------------------------------------------------------------------- #
@app.get("/zones")
def zones(
    site_id: Optional[str] = Query(None, description="Campus site id"),
    zone_id: List[str] = Query(default=[], description="Zone id (repeatable)"),
):
    sid = site_id or _get_client().site_id
    zone_ids = zone_id or None
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("zones params: site_id=%s zone_ids=%s", sid, zone_ids)
    return _get_client().info_zones(sid, zone_ids)


@app.get("/zones/summary")
def zones_summary(
    site_id: Optional[str] = Query(None, description="Campus site id"),
    zone_id: List[str] = Query(default=[], description="Zone id (repeatable)"),
):
    """Normalize zone info into a compact availability table."""
    sid = site_id or _get_client().site_id
    zone_ids = zone_id or None
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("zones/summary params: site_id=%s zone_ids=%s", sid, zone_ids)
    data = _get_client().info_zones(sid, zone_ids)
    out: List[Dict[str, Any]] = []
    for z in _as_records(data, "zones"):
        if not isinstance(z, dict):
            continue
        out.append({
            "site_id": z.get("site_id") or z.get("siteId"),
            "zone_id": z.get("zone_id") or z.get("zoneId"),
            "name": z.get("display_name") or z.get("name"),
            "occupied": z.get("occupied"),
            "vacant": z.get("vacant"),
            "total": z.get("total"),
            "status": z.get("status"),
            "lat": z.get("latitude"),
            "lng": z.get("longitude"),
        })
    logger.info("zones/summary site_id=%s -> %d zones", sid, len(out))
    return {"site_id": sid, "zones": out}


# --------------------------------------------------------------------------- #
# Stalls
# --------------------------------------------------------------------------- #
@app.get("/stalls")
def stalls(
    site_id: Optional[str] = Query(None, description="Campus site id"),
    zone_id: Optional[str] = Query(None, description="Zone id"),
    stall_id: Optional[str] = Query(None, description="Stall id"),
):
    sid = site_id or _get_client().site_id
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("stalls params: site_id=%s zone_id=%s stall_id=%s",
                     sid, zone_id, stall_id)
    return _get_client().status_stalls(sid, zone_id, stall_id)


@app.get("/stalls/summary")
def stalls_summary(
    site_id: Optional[str] = Query(None, description="Campus site id"),
    zone_id: Optional[str] = Query(None, description="Zone id"),
    stall_id: Optional[str] = Query(None, description="Stall id"),
):
    sid = site_id or _get_client().site_id
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("stalls/summary params: site_id=%s zone_id=%s stall_id=%s",
                     sid, zone_id, stall_id)
    data = _get_client().status_stalls(sid, zone_id, stall_id)
    out: List[Dict[str, Any]] = []
    for s in _as_records(data, "stalls"):
        if not isinstance(s, dict):
            continue
        out.append({
            "site_id": s.get("site_id") or s.get("siteId"),
            "zone_id": s.get("zone_id") or s.get("zoneId"),
            "stall_id": s.get("stall_id") or s.get("stallId"),
            "status": s.get("status") or s.get("cur_status"),
        })
    logger.info("stalls/summary site_id=%s -> %d stalls", sid, len(out))
    return {"site_id": sid, "stalls": out}


# --------------------------------------------------------------------------- #
# Nearby
# --------------------------------------------------------------------------- #
@app.get("/nearby")
def nearby(
    lat: Optional[str] = Query(None, description="Latitude"),
    lng: Optional[str] = Query(None, description="Longitude"),
    radius: Optional[str] = Query(None, description="Search radius (metres)"),
):
    lat_f = _lenient_float(lat, FAU_DEFAULT_LAT)
    lng_f = _lenient_float(lng, FAU_DEFAULT_LNG)
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("nearby params: lat=%s lng=%s radius=%s", lat, lng, radius)
    return _get_client().info_nearby(
        lat=lat_f, lng=lng_f,
        radius=int(radius) if radius else None,
    )


# --------------------------------------------------------------------------- #
# Payment providers
# --------------------------------------------------------------------------- #
@app.get("/payment/providers")
def payment_providers(
    site_id: Optional[str] = Query(None, description="Campus site id"),
):
    sid = site_id or _get_client().site_id
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("payment/providers params: site_id=%s", sid)
    return _get_client().payment_providers(sid)


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #
def create_app() -> FastAPI:
    return app


if __name__ == "__main__":
    host = os.environ.get("FAU_HOST", "127.0.0.1")
    port = int(os.environ.get("FAU_PORT", "5000"))
    logger.info("starting FAU parking REST API on %s:%s (log level=%s)",
                host, port, _LOG_LEVEL)
    uvicorn.run("fau_parking.rest_api:app", host=host, port=port,
                log_level=_LOG_LEVEL.lower(), reload=False)
