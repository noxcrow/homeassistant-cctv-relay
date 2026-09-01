"""CCTV Relay integration for Home Assistant OS."""

from __future__ import annotations

import logging

from homeassistant.components import persistent_notification, webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import (
    CONF_WEBHOOK_ID,
    DOMAIN,
    PLATFORMS,
)
from .runtime import CCTVRelayRuntime

_LOGGER = logging.getLogger(__name__)
_NOTIFICATION_PREFIX = "cctv_relay_webhooks"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up CCTV Relay from a config entry."""
    runtime = CCTVRelayRuntime(hass, entry)
    try:
        await runtime.async_start()
    except Exception as exc:
        raise ConfigEntryNotReady(
            f"Unable to initialize the CCTV Relay queue: {exc}"
        ) from exc

    entry.runtime_data = runtime
    webhook_id = str(entry.data[CONF_WEBHOOK_ID])
    webhook.async_register(
        hass,
        DOMAIN,
        "CCTV Relay",
        webhook_id,
        runtime.async_handle_webhook,
        local_only=True,
        allowed_methods=("PUT",),
    )

    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        webhook.async_unregister(hass, webhook_id)
        await runtime.async_stop()
        raise

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a CCTV Relay config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False

    webhook.async_unregister(hass, str(entry.data[CONF_WEBHOOK_ID]))
    await entry.runtime_data.async_stop()
    # Clean up both the legacy repeated-notice ID and the one-time setup notice ID.
    persistent_notification.async_dismiss(
        hass, f"{_NOTIFICATION_PREFIX}_{entry.entry_id}"
    )
    persistent_notification.async_dismiss(
        hass, f"{_NOTIFICATION_PREFIX}_{entry.data[CONF_WEBHOOK_ID]}"
    )
    return True
