"""FAU Parking availability client, REST API and MCP server.

This package wraps the ElevenX / ExactPark "navhub" API that powers the
official FAU Parking Android application (package
``com.elevenx.exactpark.fau``). The API contract (base URL, HMAC signing
scheme, endpoints and JSON shapes) was recovered by reverse-engineering that
application; see ``analysis/REVERSE_ENGINEERING.md`` for the full write-up.
"""

__version__ = "0.1.0"

from .client import (
    ElevenXError,
    ElevenXClient,
    FAU_DEFAULT_LAT,
    FAU_DEFAULT_LNG,
    FAU_DEFAULT_SITE_ID,
)

__all__ = [
    "ElevenXClient",
    "ElevenXError",
    "FAU_DEFAULT_LAT",
    "FAU_DEFAULT_LNG",
    "FAU_DEFAULT_SITE_ID",
    "__version__",
]
