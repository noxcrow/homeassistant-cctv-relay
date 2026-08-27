"""Config flow for CCTV Relay."""

from __future__ import annotations

import logging
import socket
import ssl
import time
from typing import Any
import urllib.error
import urllib.parse

import voluptuous as vol

from homeassistant.components import webhook
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_CAMERA_IDS,
    CONF_HISTORY_ENABLED,
    CONF_HISTORY_INTERVAL,
    CONF_POST_SECONDS,
    CONF_PRE_SECONDS,
    CONF_SYNOLOGY_URL,
    CONF_TELEGRAM_NOTIFY_ENTITY,
    CONF_VERIFY_SSL,
    CONF_WEBHOOK_ID,
    DEFAULT_DSM_TIMEOUT_SECONDS,
    DEFAULT_EXPORT_TIMEOUT_SECONDS,
    DEFAULT_HISTORY_ENABLED,
    DEFAULT_HISTORY_INTERVAL,
    DEFAULT_HISTORY_PAGE_SIZE,
    DEFAULT_POST_SECONDS,
    DEFAULT_PRE_SECONDS,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
    MAX_CLIP_SECONDS,
    MAX_POST_SECONDS,
    MAX_PRE_SECONDS,
)
from .relay import ConfigError, RelayError, SynologyAPIError, SynologyClient

_LOGGER = logging.getLogger(__name__)


def _required_field(
    defaults: dict[str, Any], key: str
) -> vol.Marker:
    if key in defaults:
        return vol.Required(key, default=defaults[key])
    return vol.Required(key)


def _bounded_int_default(
    defaults: dict[str, Any], key: str, fallback: int, minimum: int, maximum: int
) -> int:
    try:
        value = int(defaults.get(key, fallback))
    except (TypeError, ValueError):
        value = fallback
    return max(minimum, min(maximum, value))


def _base_schema(defaults: dict[str, Any]) -> vol.Schema:
    pre_default = _bounded_int_default(
        defaults, CONF_PRE_SECONDS, DEFAULT_PRE_SECONDS, 0, MAX_PRE_SECONDS
    )
    post_default = _bounded_int_default(
        defaults, CONF_POST_SECONDS, DEFAULT_POST_SECONDS, 1, MAX_POST_SECONDS
    )
    if pre_default + post_default > MAX_CLIP_SECONDS:
        post_default = max(1, MAX_CLIP_SECONDS - pre_default)
    return vol.Schema(
        {
            _required_field(
                defaults, CONF_SYNOLOGY_URL
            ): TextSelector(
                TextSelectorConfig(type=TextSelectorType.URL)
            ),
            vol.Required(
                CONF_VERIFY_SSL,
                default=defaults.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL),
            ): bool,
            _required_field(defaults, CONF_USERNAME): TextSelector(
                TextSelectorConfig(autocomplete="username")
            ),
            _required_field(defaults, CONF_PASSWORD): TextSelector(
                TextSelectorConfig(
                    type=TextSelectorType.PASSWORD,
                    autocomplete="current-password",
                )
            ),
            _required_field(
                defaults, CONF_TELEGRAM_NOTIFY_ENTITY
            ): EntitySelector(
                EntitySelectorConfig(
                    domain="notify", integration="telegram_bot"
                )
            ),
            vol.Required(
                CONF_PRE_SECONDS,
                default=pre_default,
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0,
                    max=MAX_PRE_SECONDS,
                    step=1,
                    unit_of_measurement="s",
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_POST_SECONDS,
                default=post_default,
            ): NumberSelector(
                NumberSelectorConfig(
                    min=1,
                    max=MAX_POST_SECONDS,
                    step=1,
                    unit_of_measurement="s",
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_HISTORY_ENABLED,
                default=defaults.get(
                    CONF_HISTORY_ENABLED, DEFAULT_HISTORY_ENABLED
                ),
            ): bool,
            vol.Required(
                CONF_HISTORY_INTERVAL,
                default=defaults.get(
                    CONF_HISTORY_INTERVAL, DEFAULT_HISTORY_INTERVAL
                ),
            ): vol.All(vol.Coerce(int), vol.Range(min=15, max=3600)),
        }
    )


def _camera_schema(
    defaults: dict[str, Any], camera_names: dict[int, str]
) -> vol.Schema:
    options = [
        {"value": str(camera_id), "label": f"{name} (ID {camera_id})"}
        for camera_id, name in sorted(
            camera_names.items(), key=lambda item: item[1].casefold()
        )
    ]
    selected = []
    for value in defaults.get(CONF_CAMERA_IDS, []):
        try:
            camera_id = int(value)
        except (TypeError, ValueError):
            continue
        if camera_id in camera_names:
            selected.append(str(camera_id))

    field = (
        vol.Required(CONF_CAMERA_IDS, default=selected)
        if selected
        else vol.Required(CONF_CAMERA_IDS)
    )
    return vol.Schema(
        {
            field: SelectSelector(
                SelectSelectorConfig(
                    options=options,
                    multiple=True,
                    custom_value=False,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )
        }
    )


def _build_client(data: dict[str, Any]) -> SynologyClient:
    return SynologyClient(
        str(data[CONF_SYNOLOGY_URL]),
        None,
        DEFAULT_DSM_TIMEOUT_SECONDS,
        DEFAULT_EXPORT_TIMEOUT_SECONDS,
        bool(data[CONF_VERIFY_SSL]),
    )


def _load_dsm_cameras(data: dict[str, Any]) -> dict[int, str]:
    client = _build_client(data)
    username = str(data[CONF_USERNAME])
    password = str(data[CONF_PASSWORD])
    client.discover()
    camera_names = client.camera_names(username, password)
    if not camera_names:
        _LOGGER.warning(
            "CCTV Relay found no Surveillance Station cameras accessible to the configured DSM account"
        )
        raise ValueError("no_cameras")
    if bool(data[CONF_HISTORY_ENABLED]):
        client.list_history(username, password, DEFAULT_HISTORY_PAGE_SIZE)
    return camera_names


def _validate_selected_cameras(data: dict[str, Any]) -> None:
    camera_ids = [int(value) for value in data.get(CONF_CAMERA_IDS, [])]
    if not camera_ids:
        raise ValueError("no_camera_selected")
    if len(camera_ids) != len(set(camera_ids)):
        raise ValueError("duplicate_cameras")

    client = _build_client(data)
    username = str(data[CONF_USERNAME])
    password = str(data[CONF_PASSWORD])
    camera_names = client.camera_names(username, password)
    if not set(camera_ids).issubset(camera_names):
        raise ValueError("camera_not_found")

    now = int(time.time())
    for camera_id in camera_ids:
        client.list_recordings(
            username, password, camera_id, now - 3600, now
        )


async def _async_prepare_base_input(
    hass: HomeAssistant, user_input: dict[str, Any]
) -> tuple[dict[str, Any], dict[int, str]]:
    data = dict(user_input)
    base_url = str(data[CONF_SYNOLOGY_URL]).strip().rstrip("/")
    parsed = urllib.parse.urlparse(base_url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("invalid_url")

    notify_entity = str(data[CONF_TELEGRAM_NOTIFY_ENTITY]).strip().lower()
    if (
        not notify_entity.startswith("notify.")
        or hass.states.get(notify_entity) is None
        or not hass.services.has_service("telegram_bot", "send_video")
        or not hass.services.has_service("telegram_bot", "send_message")
    ):
        raise ValueError("telegram_not_ready")

    data[CONF_SYNOLOGY_URL] = base_url
    data[CONF_USERNAME] = str(data[CONF_USERNAME]).strip()
    data[CONF_PASSWORD] = str(data[CONF_PASSWORD])
    data[CONF_TELEGRAM_NOTIFY_ENTITY] = notify_entity
    data[CONF_PRE_SECONDS] = int(data[CONF_PRE_SECONDS])
    data[CONF_POST_SECONDS] = int(data[CONF_POST_SECONDS])
    if data[CONF_PRE_SECONDS] + data[CONF_POST_SECONDS] > MAX_CLIP_SECONDS:
        raise ValueError("clip_duration_too_long")
    if not data[CONF_USERNAME] or not data[CONF_PASSWORD]:
        raise SynologyAPIError(400, "login")

    camera_names = await hass.async_add_executor_job(_load_dsm_cameras, data)
    return data, camera_names


async def _async_validate_camera_selection(
    hass: HomeAssistant, base_data: dict[str, Any], user_input: dict[str, Any]
) -> dict[str, Any]:
    data = dict(base_data)
    selected = user_input.get(CONF_CAMERA_IDS, [])
    if not isinstance(selected, list) or not selected:
        raise ValueError("no_camera_selected")
    data[CONF_CAMERA_IDS] = [int(value) for value in selected]
    await hass.async_add_executor_job(_validate_selected_cameras, data)
    return data


def _error_key(exc: Exception) -> str:
    if isinstance(exc, ValueError) and str(exc) in {
        "invalid_url",
        "camera_not_found",
        "no_cameras",
        "no_camera_selected",
        "duplicate_cameras",
        "clip_duration_too_long",
        "telegram_not_ready",
    }:
        return str(exc)
    if isinstance(exc, SynologyAPIError):
        operation = exc.operation.lower()
        if operation == "login":
            if exc.code in {403, 404, 406}:
                return "otp_required"
            if exc.code in {407, 411}:
                return "account_locked"
            if exc.code in {408, 409, 410}:
                return "password_change_required"
            if exc.code == 402:
                return "insufficient_permissions"
            return "invalid_auth"
        if exc.code == 105:
            return "insufficient_permissions"
        if operation == "list":
            return "recording_check_failed"
        if operation == "listhistory":
            return "history_check_failed"
        return "synology_api_error"
    if isinstance(exc, ConfigError):
        return "missing_apis"
    if isinstance(exc, RelayError):
        error_text = _exception_text(exc)
        if _chain_contains(exc, ssl.SSLCertVerificationError) or any(
            marker in error_text
            for marker in (
                "certificate verify failed",
                "certificate_verify_failed",
                "hostname mismatch",
            )
        ):
            return "tls_error"
        if _chain_contains(exc, socket.gaierror) or any(
            marker in error_text
            for marker in (
                "name or service not known",
                "temporary failure in name resolution",
                "nodename nor servname",
            )
        ):
            return "dns_error"
        if _chain_contains(exc, (TimeoutError, socket.timeout)) or any(
            marker in error_text for marker in ("timed out", "timeout")
        ):
            return "timeout_error"
        if "synology http 403" in error_text:
            return "webapi_forbidden"
        if any(
            marker in error_text
            for marker in (
                "invalid json response",
                "json response exceeded",
            )
        ):
            return "invalid_webapi_response"
        return "cannot_connect"
    return "unknown"


def _exception_chain(exc: BaseException) -> list[BaseException]:
    """Return wrapped exceptions and URL reasons without looping."""
    pending: list[BaseException] = [exc]
    result: list[BaseException] = []
    seen: set[int] = set()
    while pending:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        result.append(current)
        if isinstance(current, urllib.error.URLError) and isinstance(
            current.reason, BaseException
        ):
            pending.append(current.reason)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        elif current.__context__ is not None:
            pending.append(current.__context__)
    return result


def _chain_contains(
    exc: BaseException,
    exception_type: type[BaseException] | tuple[type[BaseException], ...],
) -> bool:
    return any(
        isinstance(item, exception_type) for item in _exception_chain(exc)
    )


def _exception_text(exc: BaseException) -> str:
    return " | ".join(str(item).lower() for item in _exception_chain(exc))


def _log_validation_error(context: str, error: str, exc: Exception) -> None:
    """Log actionable DSM details without user input or credentials."""
    if error == "unknown":
        _LOGGER.exception("Unexpected CCTV Relay %s error", context)
        return
    _LOGGER.warning(
        "CCTV Relay %s failed [%s]: %s", context, error, exc
    )


class CCTVRelayConfigFlow(ConfigFlow, domain=DOMAIN):
    """Configure the HAOS-native CCTV relay."""

    VERSION = 1

    def __init__(self) -> None:
        self._pending_data: dict[str, Any] = {}
        self._camera_names: dict[int, str] = {}
        self._camera_defaults: dict[str, Any] = {}
        self._reconfigure = False

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")

        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                data, camera_names = await _async_prepare_base_input(
                    self.hass, user_input
                )
            except Exception as exc:
                error = _error_key(exc)
                _log_validation_error("setup validation", error, exc)
                errors["base"] = error
            else:
                self._pending_data = data
                self._camera_names = camera_names
                self._camera_defaults = {}
                self._reconfigure = False
                return await self.async_step_cameras()

        return self.async_show_form(
            step_id="user",
            data_schema=_base_schema(user_input or {}),
            errors=errors,
        )

    async def async_step_cameras(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if not self._pending_data or not self._camera_names:
            return self.async_abort(reason="cannot_connect")

        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                data = await _async_validate_camera_selection(
                    self.hass, self._pending_data, user_input
                )
            except Exception as exc:
                error = _error_key(exc)
                _log_validation_error("camera selection", error, exc)
                errors["base"] = error
                self._camera_defaults = dict(user_input)
            else:
                if self._reconfigure:
                    entry = self._get_reconfigure_entry()
                    data[CONF_WEBHOOK_ID] = entry.data[CONF_WEBHOOK_ID]
                    return self.async_update_reload_and_abort(
                        entry, data_updates=data
                    )
                data[CONF_WEBHOOK_ID] = webhook.async_generate_id()
                return self.async_create_entry(title="CCTV Relay", data=data)

        return self.async_show_form(
            step_id="cameras",
            data_schema=_camera_schema(
                self._camera_defaults, self._camera_names
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        defaults = dict(entry.data)
        defaults.pop(CONF_WEBHOOK_ID, None)

        if user_input is not None:
            try:
                data, camera_names = await _async_prepare_base_input(
                    self.hass, user_input
                )
            except Exception as exc:
                error = _error_key(exc)
                _log_validation_error("reconfigure validation", error, exc)
                errors["base"] = error
            else:
                self._pending_data = data
                self._camera_names = camera_names
                self._camera_defaults = {
                    CONF_CAMERA_IDS: entry.data.get(CONF_CAMERA_IDS, []),
                }
                self._reconfigure = True
                return await self.async_step_cameras()

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_base_schema(user_input or defaults),
            errors=errors,
        )
