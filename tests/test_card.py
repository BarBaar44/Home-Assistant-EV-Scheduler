"""The card ships inside the integration: served and loaded on every page."""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant

from custom_components.ev_trip_planner.card import (
    CARD_FILE,
    CARD_URL,
    async_register_card,
)


async def test_card_served_and_loaded(hass: HomeAssistant) -> None:
    hass.http = MagicMock(async_register_static_paths=AsyncMock())
    hass.config.components.add("frontend")
    with patch("custom_components.ev_trip_planner.card.add_extra_js_url") as add:
        url = await async_register_card(hass)
    assert url is not None
    assert re.fullmatch(rf"{re.escape(CARD_URL)}\?v=[0-9a-f]{{12}}", url)
    (configs,), _ = hass.http.async_register_static_paths.call_args
    assert configs[0].url_path == CARD_URL
    assert configs[0].path == str(CARD_FILE)
    add.assert_called_once_with(hass, url)


async def test_no_frontend_no_card(hass: HomeAssistant) -> None:
    hass.http = None
    with patch("custom_components.ev_trip_planner.card.add_extra_js_url") as add:
        assert await async_register_card(hass) is None
    add.assert_not_called()


def test_card_file_defines_the_element() -> None:
    source = CARD_FILE.read_text(encoding="utf-8")
    assert 'customElements.define("ev-trip-card"' in source
    # Loaded alongside an old HACS copy, it must not throw.
    assert 'customElements.get("ev-trip-card")' in source
