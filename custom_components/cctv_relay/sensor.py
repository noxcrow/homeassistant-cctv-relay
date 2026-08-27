"""Diagnostic queue sensor for CCTV Relay."""

from __future__ import annotations

import datetime as dt
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .runtime import CCTVRelayRuntime


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the queue sensor."""
    async_add_entities([CCTVRelayQueueSensor(entry)])


class CCTVRelayQueueSensor(SensorEntity):
    """Report unsent durable events without exposing the webhook secret."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:tray-full"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "queue"

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._runtime: CCTVRelayRuntime = entry.runtime_data
        self._attr_unique_id = f"{entry.entry_id}_queue"
        self._attr_native_value = 0
        self._attr_extra_state_attributes: dict[str, Any] = {}

    async def async_added_to_hass(self) -> None:
        """Subscribe to immediate queue refresh requests."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, self._runtime.signal, self._async_queue_changed
            )
        )

    @callback
    def _async_queue_changed(self) -> None:
        self.async_schedule_update_ha_state(force_refresh=True)

    async def async_update(self) -> None:
        """Fetch queue state outside the Home Assistant event loop."""
        summary = await self._runtime.async_summary()
        counts = summary.get("counts", {})
        self._attr_native_value = sum(
            int(counts.get(status, 0))
            for status in ("pending", "retry", "processing", "failed")
        )
        attributes: dict[str, Any] = {
            "pending": int(counts.get("pending", 0)),
            "retry": int(counts.get("retry", 0)),
            "processing": int(counts.get("processing", 0)),
            "failed": int(counts.get("failed", 0)),
            "sent": int(counts.get("sent", 0)),
        }
        if (oldest := summary.get("oldest_unsent_epoch")) is not None:
            attributes["oldest_unsent"] = dt.datetime.fromtimestamp(
                float(oldest), tz=dt.UTC
            ).isoformat()

        metadata = summary.get("metadata", {})
        if success := metadata.get("history_last_success_epoch"):
            attributes["history_last_success"] = dt.datetime.fromtimestamp(
                float(success), tz=dt.UTC
            ).isoformat()
        if error := metadata.get("history_last_error"):
            attributes["history_last_error"] = error
        self._attr_extra_state_attributes = attributes
