"""CCTV Relay integration for Home Assistant OS."""

from __future__ import annotations

import logging

from homeassistant.components import persistent_notification, webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.network import NoURLAvailableError

from .const import (
    CONF_CAMERA_IDS,
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
    """Show DSM Action Rule URLs for every selected camera."""
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

    lines = [
        "Surveillance Station Action Rule의 웹훅 URL을 아래 값으로 설정하세요.",
        "선택한 각 카메라에 필요한 이벤트 규칙만 생성하면 됩니다.",
        "이 URL에는 비밀 키가 포함되므로 외부에 공개하지 마세요.",
        "",
    ]
    runtime = entry.runtime_data
    for camera_id in entry.data.get(CONF_CAMERA_IDS, []):
        camera_key = str(int(camera_id))
        camera = runtime.cameras.get(camera_key)
        display_name = camera.display_name if camera else f"Camera ID {camera_key}"
        lines.append(f"### {display_name}")
        for event_type in ("motion", "lost", "restored"):
            rule_name = f"TG_CAMERA_{camera_key}_{event_type.upper()}"
            lines.append(
                f"- `{rule_name}`: "
                f"`{base_webhook_url}?camera={camera_key}&event_type={event_type}`"
            )
        lines.append("")

    persistent_notification.async_create(
        hass,
        "\n".join(lines),
        title="CCTV Relay 웹훅 설정",
        notification_id=f"{_NOTIFICATION_PREFIX}_{entry.entry_id}",
    )
