"""CCTV Relay integration for Home Assistant OS."""

from __future__ import annotations

import logging

from homeassistant.components import persistent_notification, webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.network import NoURLAvailableError

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
        allowed_methods=("GET", "POST", "PUT"),
    )

    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        webhook.async_unregister(hass, webhook_id)
        await runtime.async_stop()
        raise

    _async_show_webhook_notification(hass, entry)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a CCTV Relay config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False

    webhook.async_unregister(hass, str(entry.data[CONF_WEBHOOK_ID]))
    await entry.runtime_data.async_stop()
    persistent_notification.async_dismiss(
        hass, f"{_NOTIFICATION_PREFIX}_{entry.entry_id}"
    )
    return True


def _async_show_webhook_notification(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Show the six DSM Action Rule URLs without exposing them as entity state."""
    webhook_id = str(entry.data[CONF_WEBHOOK_ID])
    try:
        base_webhook_url = webhook.async_generate_url(
            hass,
            webhook_id,
            allow_internal=True,
            allow_external=False,
            prefer_external=False,
        )
    except NoURLAvailableError:
        base_webhook_url = (
            "http://HOME_ASSISTANT_IP:8123"
            f"{webhook.async_generate_path(webhook_id)}"
        )
    events = (
        ("TG_CAMERA1_MOTION", "camera1", "motion"),
        ("TG_CAMERA2_MOTION", "camera2", "motion"),
        ("TG_CAMERA1_LOST", "camera1", "lost"),
        ("TG_CAMERA1_RESTORED", "camera1", "restored"),
        ("TG_CAMERA2_LOST", "camera2", "lost"),
        ("TG_CAMERA2_RESTORED", "camera2", "restored"),
    )
    lines = [
        "Surveillance Station Action Rule의 웹훅 URL을 아래 값으로 설정하세요.",
        "이 URL에는 비밀 키가 포함되므로 외부에 공개하지 마세요.",
        "",
    ]
    for rule_name, camera, event_type in events:
        lines.append(
            f"- `{rule_name}`: "
            f"`{base_webhook_url}?camera={camera}&event_type={event_type}`"
        )
    persistent_notification.async_create(
        hass,
        "\n".join(lines),
        title="CCTV Relay 웹훅 설정",
        notification_id=f"{_NOTIFICATION_PREFIX}_{entry.entry_id}",
    )
