"""Serve the EV Trip Card from the integration.

HACS gives a repository one category, so the card ships inside the
integration rather than as a Dashboard repository of its own. The file is
served from /ev_trip_planner/ev-trip-card.js and loaded on every frontend
page as an extra module, so no Lovelace resource has to be added by hand
(and dashboards in YAML mode work the same). The query string is a hash of
the file: a new card reaches the browser on the next reload, with no
version number to keep in step."""

from __future__ import annotations

import hashlib
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant

from .const import DOMAIN, LOGGER

CARD_FILE = Path(__file__).parent / "frontend" / "ev-trip-card.js"
CARD_URL = f"/{DOMAIN}/ev-trip-card.js"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


async def async_register_card(hass: HomeAssistant) -> str | None:
    """Serve the card and load it on every page. The URL it is loaded
    from, or None when there is no frontend to load it into."""
    if hass.http is None or "frontend" not in hass.config.components:
        LOGGER.debug("No frontend, the EV Trip Card is not served")
        return None
    digest = await hass.async_add_executor_job(_digest, CARD_FILE)
    await hass.http.async_register_static_paths(
        [StaticPathConfig(CARD_URL, str(CARD_FILE), cache_headers=True)]
    )
    url = f"{CARD_URL}?v={digest}"
    add_extra_js_url(hass, url)
    return url
