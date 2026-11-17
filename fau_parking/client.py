"""Thin, typed client for the FAU Parking (ElevenX/ExactPark "navhub") API.

The upstream API is HMAC-signed: every request must carry an ``Authorization``
header, an ``X-Timestamp`` header and a ``Cache-Control`` header. The signature
is ``HMAC_SHA256(key, "<path-from-api>?<query>|<unix-seconds>")`` rendered as
hex, where ``<path-from-api>`` is the request path beginning at ``api`` (both
the base URL host *and* the leading ``/`` are stripped, matching
``str.substring(indexOf("/api") + 1)`` in the decompiled
``ElevenXApiServiceKt`` class) and ``<query>`` is the exact query string sent
on the wire. This mirrors the ``AuthorizationInterceptor`` in that class.

The ``site_id`` is not hardcoded in the app; it is discovered at runtime from
``api/v2/info/nearby``. We therefore expose a small helper to resolve it, and
fall back to a configurable default.
"""

from __future__ import annotations

import hashlib
import hmac
import time
import urllib.parse
import uuid as _uuid
from typing import Any, Dict, List, Optional

import requests

BASE_URL = "https://fau-navhub.exactpark.com/"

# Recovered from the decompiled ``ElevenXApiServiceKt`` class.
HMAC_KEY = "iuHfjcq5x6rKQmqT4Ox8Q7AxIlfBWzd9c2eBIEuij5qYqZewiNbd2Ukt9RnBoj84"
HMAC_ALG = "HmacSHA256"

# Cache freshness the app requests (seconds).
CACHE_MAX_AGE = 60

# FAU Boca Raton campus, used as the default lookup centre when no coordinates
# are supplied. The app derives these from the device location, so they are a
# convenience default rather than a protocol constant.
FAU_DEFAULT_LAT = 26.3727
FAU_DEFAULT_LNG = -80.1020

# The site_id is dynamic. This default is the value observed for the FAU Boca
# Raton campus; it is overridable via the constructor or environment.
FAU_DEFAULT_SITE_ID = "fau"


class ElevenXError(RuntimeError):
    """Raised when the navhub API returns an error status."""

    def __init__(self, status: int, path: str, body: str):
        self.status = status
        self.path = path
        self.body = body
        super().__init__(f"{path} -> HTTP {status}: {body[:200]}")


class ElevenXClient:
    """Synchronous client for the FAU Parking navhub API."""

    def __init__(
        self,
        base_url: str = BASE_URL,
        hmac_key: str = HMAC_KEY,
        site_id: str = FAU_DEFAULT_SITE_ID,
        lang: str = "en-CA",
        client_id: str = _uuid.uuid4().hex,
        use_12_hour_time: bool = True,
        timeout: float = 15.0,
        session: Optional[requests.Session] = None,
    ):
        self.base_url = base_url.rstrip("/") + "/"
        self.hmac_key = hmac_key
        self.site_id = site_id
        self.lang = lang
        # The app sends ``client_id`` (its device UUID) on every request;
        # without it the server answers with an HTML landing page instead of
        # JSON. The app generates a random UUID on first launch, so we default
        # to one here; pass an explicit value to reuse a stable id. Recovered
        # from MainActivity / ElevenXApiServiceKt.
        self.client_id = client_id
        self.use_12_hour_time = use_12_hour_time
        self.timeout = timeout
        self._session = session or requests.Session()

    # ------------------------------------------------------------------ #
    # Signing
    # ------------------------------------------------------------------ #
    def _sign(self, message: str, ts: int) -> str:
        # The app signs the request path beginning at "api" (the base URL host
        # *and* the leading "/" are stripped, see getUnsignedHmacAuth:
        # ``str.substring(indexOf("/api") + 1)``), with the query string
        # appended, joined to the unix timestamp with "|". The signed message
        # is therefore ``"api/v2/<endpoint>?<query>|<unix-seconds>"``.
        signed = f"{message}|{ts}"
        digest = hmac.new(
            self.hmac_key.encode("utf-8"),
            signed.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return digest

    def _headers(self, signature: str, ts: int) -> Dict[str, str]:
        return {
            "Authorization": signature,
            "X-Timestamp": str(ts),
            "Cache-Control": f"public, max-age={CACHE_MAX_AGE}",
        }

    # ------------------------------------------------------------------ #
    # Core request helper
    # ------------------------------------------------------------------ #
    # Common params
    # ------------------------------------------------------------------ #
    def _common(self, params: Dict[str, Any], *,
                lang: bool = True, use12: bool = False) -> Dict[str, Any]:
        """Append the params the app adds to most requests, in the app's order.

        Every map builder in ``ElevenXApiServiceKt`` appends ``client_id`` and
        (on most endpoints) ``lang`` and ``use12HourTime`` *after* the
        endpoint-specific params. ``use12HourTime`` is Java ``String.valueOf``
        of a bool, i.e. the lowercase ``"true"``/``"false"`` (not Python's
        ``"True"``/``"False"``). Not every endpoint sends ``lang``/``use12HourTime``
        — e.g. ``status/stalls`` sends only ``client_id`` — so both are opt-in.
        """
        out = dict(params)
        out.setdefault("client_id", self.client_id)
        if lang:
            out.setdefault("lang", self.lang)
        if use12:
            out.setdefault("use12HourTime",
                           "true" if self.use_12_hour_time else "false")
        return out

    # ------------------------------------------------------------------ #
    # Core request helper
    # ------------------------------------------------------------------ #
    def _get(self, path: str, params: Dict[str, Any]) -> Any:
        ts = int(time.time())
        # Build the query string once so the signed message matches exactly the
        # query that goes on the wire (the app signs request.url().toString()).
        # ``urlencode`` preserves dict insertion order, so the signed string
        # matches the one sent.
        encoded = urllib.parse.urlencode(params)
        signed_path = path.lstrip("/")  # drop the leading "/" -> "api/v2/..."
        signed_message = signed_path if not encoded else f"{signed_path}?{encoded}"
        signature = self._sign(signed_message, ts)
        headers = self._headers(signature, ts)
        url = self.base_url + signed_path
        if encoded:
            url = f"{url}?{encoded}"
        resp = self._session.get(
            url,
            headers=headers,
            timeout=self.timeout,
        )
        if resp.status_code >= 400:
            raise ElevenXError(resp.status_code, path, resp.text)
        try:
            return resp.json()
        except ValueError:
            return resp.text

    # ------------------------------------------------------------------ #
    # Site discovery
    # ------------------------------------------------------------------ #
    def discover_site_id(self, lat: float = FAU_DEFAULT_LAT,
                         lng: float = FAU_DEFAULT_LNG,
                         radius: Optional[int] = None) -> str:
        """Return the site_id for the campus nearest the given coordinates.

        Uses ``api/v2/info/nearby`` which returns zone records; each record
        carries a ``site_id``. We return the first one found.
        """
        # The app sends latitude/longitude (not lat/lng) and no site_id here.
        params: Dict[str, Any] = {"latitude": lat, "longitude": lng}
        if radius is not None:
            params["radius"] = radius
        data = self._get("api/v2/info/nearby",
                         self._common(params, lang=True, use12=True))
        records = data if isinstance(data, list) else data.get("zones", data)
        if isinstance(records, list) and records:
            first = records[0]
            if isinstance(first, dict) and "site_id" in first:
                return str(first["site_id"])
            if isinstance(first, dict) and "siteId" in first:
                return str(first["siteId"])
        # Fall back to configured default.
        return self.site_id

    # ------------------------------------------------------------------ #
    # Endpoints
    # ------------------------------------------------------------------ #
    def info_zones(self, site_id: Optional[str] = None,
                   zone_ids: Optional[List[str]] = None) -> Any:
        """``GET api/v2/info/zones`` — zone metadata + current occupancy.

        The app sends ``site_id``, ``zones`` (comma-joined, not ``zone_ids``),
        ``client_id`` and ``lang`` — but no ``use12HourTime`` (see
        ``getZoneStatus`` in ``ElevenXApiServiceKt``).
        """
        params: Dict[str, Any] = {"site_id": site_id or self.site_id}
        if zone_ids:
            params["zones"] = ",".join(zone_ids)
        return self._get("api/v2/info/zones",
                         self._common(params, lang=True, use12=False))

    def info_nearby(self, lat: float = FAU_DEFAULT_LAT,
                    lng: float = FAU_DEFAULT_LNG,
                    radius: Optional[int] = None) -> Any:
        """``GET api/v2/info/nearby`` — zones near a coordinate."""
        params: Dict[str, Any] = {"latitude": lat, "longitude": lng}
        if radius is not None:
            params["radius"] = radius
        return self._get("api/v2/info/nearby",
                         self._common(params, lang=True, use12=True))

    def status_stalls(self, site_id: Optional[str] = None,
                      zone_id: Optional[str] = None,
                      stall_id: Optional[str] = None) -> Any:
        """``GET api/v2/status/stalls`` — live per-stall occupancy.

        The app sends only ``site_id``, ``stall_id`` and ``client_id`` here —
        no ``lang`` and no ``use12HourTime``. ``zone_id`` is accepted for API
        symmetry but the app does not send it.
        """
        params: Dict[str, Any] = {"site_id": site_id or self.site_id}
        if stall_id:
            params["stall_id"] = stall_id
        return self._get("api/v2/status/stalls",
                         self._common(params, lang=False, use12=False))

    def alternative_zones(self, site_id: Optional[str] = None,
                          lat: float = FAU_DEFAULT_LAT,
                          lng: float = FAU_DEFAULT_LNG) -> Any:
        """``GET api/v2/alternative/zones``.

        The app sends ``site_id``, ``zone_id``, ``latitude``/``longitude``
        (not ``lat``/``lng``), a fixed ``radius=5000`` metres, then
        ``client_id``, ``lang`` and ``use12HourTime``.
        """
        params: Dict[str, Any] = {
            "site_id": site_id or self.site_id,
            "latitude": lat,
            "longitude": lng,
            "radius": 5000,
        }
        return self._get("api/v2/alternative/zones",
                         self._common(params, lang=True, use12=True))

    def alternative_stalls(self, site_id: Optional[str] = None,
                           zone_id: Optional[str] = None,
                           stall_id: Optional[str] = None) -> Any:
        """``GET api/v2/alternative/stalls``.

        The app sends ``site_id``, ``zone_id``, ``stall_id``, then
        ``client_id``, ``lang`` and ``use12HourTime``.
        """
        params: Dict[str, Any] = {"site_id": site_id or self.site_id}
        if zone_id:
            params["zone_id"] = zone_id
        if stall_id:
            params["stall_id"] = stall_id
        return self._get("api/v2/alternative/stalls",
                         self._common(params, lang=True, use12=True))

    def payment_providers(self, site_id: Optional[str] = None) -> Any:
        """``GET api/v2/payment/providers``.

        The app builds an empty param map for this endpoint, so no ``site_id``
        is sent — only the ``client_id`` default is added (no ``lang``/
        ``use12HourTime``). ``site_id`` is accepted for API symmetry but
        ignored.
        """
        return self._get("api/v2/payment/providers", self._common({}))
