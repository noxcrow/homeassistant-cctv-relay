"""Home Assistant runtime for the CCTV Relay integration."""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
import dataclasses
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import json
import logging
import pathlib
import random
import re
import shutil
import sqlite3
import tempfile
import time
import urllib.parse
from typing import Any, Callable, TypeVar

from aiohttp import web

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    ALLOWED_EVENT_TYPES,
    CONF_CAMERA_IDS,
    CONF_FFMPEG_PATH,
    CONF_HISTORY_ENABLED,
    CONF_HISTORY_INTERVAL,
    CONF_HISTORY_LOOKBACK,
    CONF_HISTORY_PAGE_SIZE,
    CONF_MATCH_WINDOW,
    CONF_MAX_CLIP_MEGABYTES,
    CONF_POST_SECONDS,
    CONF_PRE_SECONDS,
    CONF_SYNOLOGY_URL,
    CONF_TELEGRAM_NOTIFY_ENTITY,
    CONF_VERIFY_SSL,
    DEFAULT_DSM_TIMEOUT_SECONDS,
    DEFAULT_EXPORT_TIMEOUT_SECONDS,
    DEFAULT_FFMPEG_PATH,
    DEFAULT_HISTORY_ENABLED,
    DEFAULT_HISTORY_INTERVAL,
    DEFAULT_HISTORY_LOOKBACK,
    DEFAULT_HISTORY_PAGE_SIZE,
    DEFAULT_INDEX_GRACE_SECONDS,
    DEFAULT_MATCH_WINDOW,
    DEFAULT_MAX_ACTIVE_EVENTS,
    DEFAULT_MAX_RETRY_ATTEMPTS,
    DEFAULT_MAX_CLIP_MEGABYTES,
    DEFAULT_POST_SECONDS,
    DEFAULT_PRE_SECONDS,
    DEFAULT_RETRY_BASE_SECONDS,
    DEFAULT_RETRY_MAX_SECONDS,
    DEFAULT_SENT_RETENTION_DAYS,
    DEFAULT_WEBHOOK_RATE_LIMIT,
    DEFAULT_WEBHOOK_RATE_WINDOW_SECONDS,
    DEFAULT_VERIFY_SSL,
    SIGNAL_QUEUE_UPDATED,
)
from .relay import (
    CameraConfig,
    EventStore,
    QueueFullError,
    RelayError,
    SynologyClient,
    normalize_export,
    parse_event_time,
)

_LOGGER = logging.getLogger(__name__)

MAX_WEBHOOK_BODY_BYTES = 64 * 1024
_T = TypeVar("_T")


def _prepare_temp_root(path: pathlib.Path) -> None:
    """Remove clips left by an interrupted Core process."""
    path.mkdir(parents=True, exist_ok=True)
    for child in path.iterdir():
        try:
            if child.is_symlink() or child.is_file():
                child.unlink()
            elif child.is_dir():
                shutil.rmtree(child)
        except FileNotFoundError:
            pass


def _parse_rule_name(rule_name: str) -> tuple[str, str] | None:
    """Parse TG_CAMERA_<DSM_ID>_<EVENT> Action Rule names."""
    match = re.fullmatch(
        r"TG_CAMERA_(\d+)_(MOTION|LOST|RESTORED)", rule_name.upper()
    )
    if match is None:
        return None
    return match.group(1), match.group(2).lower()


class CCTVRelayRuntime:
    """Own the durable queue, workers, and Synology client for one entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.data = dict(entry.data)
        self.store: EventStore | None = None
        self.synology: SynologyClient | None = None
        camera_ids = [int(value) for value in self.data.get(CONF_CAMERA_IDS, [])]
        if not camera_ids:
            raise RelayError("no cameras configured")
        self.cameras = {
            str(camera_id): CameraConfig(
                key=str(camera_id),
                camera_id=camera_id,
                slot_name=f"카메라 ID {camera_id}",
            )
            for camera_id in camera_ids
        }
        self._db_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="cctv-relay-db"
        )
        self._tasks: list[asyncio.Task[Any]] = []
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._started = False
        self._temp_root = pathlib.Path(hass.config.path("temp", "cctv_relay"))
        self._signal = f"{SIGNAL_QUEUE_UPDATED}_{entry.entry_id}"
        self._webhook_rate: dict[str, deque[float]] = defaultdict(deque)

    @property
    def signal(self) -> str:
        """Dispatcher signal emitted whenever queue state changes."""
        return self._signal

    async def _async_db(
        self, function: Callable[..., _T], *args: Any, **kwargs: Any
    ) -> _T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._db_executor, partial(function, *args, **kwargs)
        )

    async def _async_cancellation_safe_executor_job(
        self, function: Callable[..., _T], *args: Any
    ) -> _T:
        """Let a blocking job finish before propagating task cancellation."""
        future = self.hass.async_add_executor_job(function, *args)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(future)
            except Exception:
                _LOGGER.exception(
                    "CCTV blocking job failed while the integration was stopping"
                )
            raise

    def _require_store(self) -> EventStore:
        if self.store is None:
            raise RuntimeError("CCTV Relay queue is not initialized")
        return self.store

    def _require_synology(self) -> SynologyClient:
        if self.synology is None:
            raise RuntimeError("CCTV Relay Synology client is not initialized")
        return self.synology

    async def async_start(self) -> None:
        """Initialize durable state and start one worker per camera."""
        if self._started:
            return
        database_path = self.hass.config.path(
            ".storage", f"cctv_relay_{self.entry.entry_id}.db"
        )
        try:
            self.synology = await self.hass.async_add_executor_job(
                SynologyClient,
                str(self.data[CONF_SYNOLOGY_URL]),
                None,
                DEFAULT_DSM_TIMEOUT_SECONDS,
                DEFAULT_EXPORT_TIMEOUT_SECONDS,
                bool(self.data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)),
            )
            await self.hass.async_add_executor_job(
                _prepare_temp_root, self._temp_root
            )
            self.store = await self._async_db(EventStore, database_path)

            try:
                camera_names = await self.hass.async_add_executor_job(
                    self.synology.camera_names,
                    str(self.data[CONF_USERNAME]),
                    str(self.data[CONF_PASSWORD]),
                )
            except RelayError as exc:
                _LOGGER.warning(
                    "Could not load Surveillance Station camera names; "
                    "using configured ID labels: %s",
                    exc,
                )
            else:
                self.cameras = {
                    key: dataclasses.replace(
                        camera, camera_name=camera_names.get(camera.camera_id)
                    )
                    for key, camera in self.cameras.items()
                }

            if bool(
                self.data.get(CONF_HISTORY_ENABLED, DEFAULT_HISTORY_ENABLED)
            ):
                summary = await self._async_db(self.store.summary)
                if not summary.get("metadata", {}).get(
                    "history_last_success_epoch"
                ):
                    await self._async_db(
                        self.store.set_metadata,
                        "history_last_success_epoch",
                        str(int(time.time())),
                    )
        except Exception:
            if self.store is not None:
                await self._async_db(self.store.close)
                self.store = None
            self.synology = None
            await self.hass.async_add_executor_job(
                partial(self._db_executor.shutdown, wait=True)
            )
            raise

        self._started = True
        for camera_key in self.cameras:
            self._tasks.append(
                self.entry.async_create_background_task(
                    self.hass,
                    self._async_camera_worker(camera_key),
                    f"CCTV Relay worker ({camera_key})",
                )
            )
        if bool(self.data.get(CONF_HISTORY_ENABLED, DEFAULT_HISTORY_ENABLED)):
            self._tasks.append(
                self.entry.async_create_background_task(
                    self.hass,
                    self._async_history_worker(),
                    "CCTV Relay history reconciliation",
                )
            )

    async def async_stop(self) -> None:
        """Stop workers after preventing new queue work, then close SQLite."""
        if not self._started:
            return
        self._stop.set()
        self._wake.set()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        store = self.store
        self.store = None
        if store is not None:
            await self._async_db(store.close)
        await self.hass.async_add_executor_job(
            partial(self._db_executor.shutdown, wait=True)
        )
        self.synology = None
        self._started = False

    async def async_summary(self) -> dict[str, Any]:
        """Return a non-blocking snapshot used by the diagnostic sensor."""
        return await self._async_db(self._require_store().summary)

    def _queue_changed(self) -> None:
        self._wake.set()
        async_dispatcher_send(self.hass, self._signal)

    def _webhook_rate_allowed(self, request: web.Request) -> bool:
        """Apply a lightweight per-peer sliding-window webhook rate limit."""
        peer = request.remote or "unknown"
        now = time.monotonic()
        bucket = self._webhook_rate[peer]
        cutoff = now - DEFAULT_WEBHOOK_RATE_WINDOW_SECONDS
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= DEFAULT_WEBHOOK_RATE_LIMIT:
            return False
        bucket.append(now)
        return True

    async def async_handle_webhook(
        self,
        _hass: HomeAssistant,
        _webhook_id: str,
        request: web.Request,
    ) -> web.Response:
        """Validate and durably enqueue a local DSM Action Rule webhook."""
        if not self._webhook_rate_allowed(request):
            return web.json_response({"error": "rate limit exceeded"}, status=429)

        content_length = request.content_length
        if content_length is not None and (
            content_length < 0 or content_length > MAX_WEBHOOK_BODY_BYTES
        ):
            return web.json_response({"error": "request body too large"}, status=413)

        try:
            body = await request.content.read(MAX_WEBHOOK_BODY_BYTES + 1)
        except (ValueError, OSError):
            return web.json_response({"error": "invalid request body"}, status=400)
        if len(body) > MAX_WEBHOOK_BODY_BYTES:
            return web.json_response({"error": "request body too large"}, status=413)

        payload: dict[str, Any] = dict(request.query)
        if body:
            try:
                if request.content_type == "application/json":
                    decoded = json.loads(body.decode("utf-8"))
                    if not isinstance(decoded, dict):
                        raise ValueError
                    payload.update(decoded)
                else:
                    form = urllib.parse.parse_qs(
                        body.decode("utf-8"), keep_blank_values=True
                    )
                    payload.update({key: values[-1] for key, values in form.items()})
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                return web.json_response({"error": "invalid request body"}, status=400)

        camera_key = str(payload.get("camera", "")).strip()
        event_type = str(payload.get("event_type", "")).strip().lower()
        if camera_key not in self.cameras:
            return web.json_response({"error": "unknown camera"}, status=400)
        if event_type not in ALLOWED_EVENT_TYPES:
            return web.json_response({"error": "unknown event type"}, status=400)

        try:
            event_time = parse_event_time(payload.get("event_time"))
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)

        external_id = str(payload.get("event_id", "")).strip()[:200]
        stored_payload = {
            "camera": camera_key,
            "event_type": event_type,
        }
        if external_id:
            stored_payload["event_id"] = external_id
        if payload.get("event_time") not in (None, ""):
            stored_payload["event_time"] = event_time
        if external_id:
            event_key = (
                f"webhook:{camera_key}:{event_type}:{external_id}"
            )
        else:
            event_key = f"webhook:{camera_key}:{event_type}:{int(event_time)}"

        try:
            event_id, created = await self._async_db(
                self._require_store().enqueue,
                event_key=event_key,
                camera_key=camera_key,
                event_type=event_type,
                event_time=event_time,
                source="webhook",
                payload=stored_payload,
                max_active_events=DEFAULT_MAX_ACTIVE_EVENTS,
                # Use the same correlation window as Action Rule history.
                # EventStore applies this fuzzy match only across sources, so
                # legitimate same-source motion bursts are not collapsed.
                match_window_seconds=int(
                    self.data.get(CONF_MATCH_WINDOW, DEFAULT_MATCH_WINDOW)
                ),
            )
        except QueueFullError:
            _LOGGER.warning("Rejected CCTV webhook because the active queue is full")
            return web.json_response({"error": "queue full"}, status=429)
        except (sqlite3.Error, RuntimeError):
            _LOGGER.exception("Could not persist CCTV webhook")
            return web.json_response({"error": "queue unavailable"}, status=503)

        if created:
            self._queue_changed()
        return web.json_response(
            {
                "accepted": True,
                "event_id": event_id,
                "deduplicated": not created,
            },
            status=202,
        )

    async def _async_camera_worker(self, camera_key: str) -> None:
        """Process one camera serially while allowing cameras in parallel."""
        store = self._require_store()
        while not self._stop.is_set():
            event = await self._async_db(store.claim_next, camera_key)
            if event is None:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=1.0)
                except TimeoutError:
                    pass
                self._wake.clear()
                continue

            try:
                await self._async_process_event(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # the durable queue must survive every failure
                requested = exc.retry_after if isinstance(exc, RelayError) else None
                attempts = int(event["attempts"]) + 1
                if attempts >= DEFAULT_MAX_RETRY_ATTEMPTS:
                    await self._async_db(
                        store.mark_failed,
                        int(event["id"]),
                        str(exc) or type(exc).__name__,
                    )
                    _LOGGER.error(
                        "CCTV event %s (%s/%s) moved to failed after %d attempts: %s",
                        event["id"],
                        camera_key,
                        event["event_type"],
                        attempts,
                        str(exc)[:500],
                    )
                    await self._async_db(
                        store.cleanup_terminal, DEFAULT_SENT_RETENTION_DAYS
                    )
                    self._queue_changed()
                    continue
                exponential = DEFAULT_RETRY_BASE_SECONDS * (
                    2 ** min(attempts, 12)
                )
                delay = min(
                    DEFAULT_RETRY_MAX_SECONDS,
                    max(exponential, requested or 0),
                )
                delay += random.uniform(0, min(1.0, delay * 0.1))
                await self._async_db(
                    store.mark_retry,
                    int(event["id"]),
                    str(exc) or type(exc).__name__,
                    time.time() + delay,
                )
                _LOGGER.error(
                    "CCTV event %s (%s/%s) failed; retrying in %.1fs: %s",
                    event["id"],
                    camera_key,
                    event["event_type"],
                    delay,
                    str(exc)[:500],
                )
            else:
                await self._async_db(store.mark_sent, int(event["id"]), None)
                await self._async_db(
                    store.cleanup_terminal, DEFAULT_SENT_RETENTION_DAYS
                )
                _LOGGER.info(
                    "CCTV event %s (%s/%s) sent",
                    event["id"],
                    camera_key,
                    event["event_type"],
                )
            self._queue_changed()

    async def _async_process_event(self, event: dict[str, Any]) -> None:
        camera = self.cameras[str(event["camera_key"])]
        event_type = str(event["event_type"])
        notify_entity = str(self.data[CONF_TELEGRAM_NOTIFY_ENTITY])

        if event_type == "motion":
            ready_at = (
                float(event["event_time"])
                + int(self.data.get(CONF_POST_SECONDS, DEFAULT_POST_SECONDS))
                + DEFAULT_INDEX_GRACE_SECONDS
            )
            if (remaining := ready_at - time.time()) > 0:
                await asyncio.sleep(remaining)

            temporary = await self.hass.async_add_executor_job(
                partial(
                    tempfile.mkdtemp,
                    prefix=f"cctv_{event['id']}_",
                    dir=self._temp_root,
                )
            )
            try:
                video = await self._async_cancellation_safe_executor_job(
                    self._export_video, event, camera, temporary
                )
                await self.hass.services.async_call(
                    "telegram_bot",
                    "send_video",
                    {
                        "entity_id": [notify_entity],
                        "file": str(video),
                        "caption": camera.caption,
                    },
                    blocking=True,
                )
            finally:
                await self.hass.async_add_executor_job(
                    partial(shutil.rmtree, temporary, ignore_errors=True)
                )
            return

        if event_type == "lost":
            message = camera.lost_message
        elif event_type == "restored":
            message = camera.restored_message
        else:
            raise RelayError(f"unsupported event type: {event_type}")

        await self.hass.services.async_call(
            "telegram_bot",
            "send_message",
            {
                "entity_id": [notify_entity],
                "message": message,
            },
            blocking=True,
        )

    def _export_video(
        self,
        event: dict[str, Any],
        camera: CameraConfig,
        temporary: str,
    ) -> pathlib.Path:
        event_time = float(event["event_time"])
        pre_seconds = int(self.data.get(CONF_PRE_SECONDS, DEFAULT_PRE_SECONDS))
        post_seconds = int(self.data.get(CONF_POST_SECONDS, DEFAULT_POST_SECONDS))
        basename = f"{camera.key}_{int(event_time)}_{event['id']}"
        exported = self._require_synology().export_range(
            username=str(self.data[CONF_USERNAME]),
            password=str(self.data[CONF_PASSWORD]),
            camera_id=camera.camera_id,
            from_time=int(event_time) - pre_seconds,
            to_time=int(event_time) + post_seconds,
            file_basename=basename,
            destination_directory=temporary,
        )
        video = normalize_export(
            exported,
            pathlib.Path(temporary),
            str(self.data.get(CONF_FFMPEG_PATH, DEFAULT_FFMPEG_PATH)),
        )
        size = video.stat().st_size
        maximum = int(
            self.data.get(CONF_MAX_CLIP_MEGABYTES, DEFAULT_MAX_CLIP_MEGABYTES)
        ) * 1024 * 1024
        if size > maximum:
            raise RelayError(
                f"video size {size} exceeds configured Telegram safety limit"
            )
        return video

    async def _async_history_worker(self) -> None:
        interval = int(
            self.data.get(CONF_HISTORY_INTERVAL, DEFAULT_HISTORY_INTERVAL)
        )
        while not self._stop.is_set():
            try:
                await self._async_reconcile_history()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = (str(exc) or type(exc).__name__).replace("\n", " ")[:1000]
                await self._async_db(
                    self._require_store().set_metadata,
                    "history_last_error",
                    error,
                )
                _LOGGER.error("CCTV history reconciliation failed: %s", error)
                self._queue_changed()

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except TimeoutError:
                pass

    async def _async_reconcile_history(self) -> None:
        page_size = int(
            self.data.get(CONF_HISTORY_PAGE_SIZE, DEFAULT_HISTORY_PAGE_SIZE)
        )
        rows = await self.hass.async_add_executor_job(
            self._require_synology().list_history,
            str(self.data[CONF_USERNAME]),
            str(self.data[CONF_PASSWORD]),
            page_size,
        )
        match_window = int(
            self.data.get(CONF_MATCH_WINDOW, DEFAULT_MATCH_WINDOW)
        )
        store = self._require_store()
        summary = await self._async_db(store.summary)
        last_success_raw = summary.get("metadata", {}).get(
            "history_last_success_epoch"
        )
        try:
            last_success = float(last_success_raw)
        except (TypeError, ValueError):
            last_success = time.time()
        cutoff = max(
            time.time()
            - int(self.data.get(CONF_HISTORY_LOOKBACK, DEFAULT_HISTORY_LOOKBACK)),
            last_success - match_window,
        )
        created_count = 0

        for row in rows:
            rule_name = str(row.get("ruleName", ""))
            mapping = _parse_rule_name(rule_name)
            if mapping is None:
                continue
            try:
                history_id = int(row["id"])
                event_time = parse_event_time(row["time"])
            except (KeyError, TypeError, ValueError):
                continue
            if event_time < cutoff:
                continue
            camera_key, event_type = mapping
            if camera_key not in self.cameras:
                continue
            _, created = await self._async_db(
                store.enqueue,
                event_key=f"history:{history_id}",
                camera_key=camera_key,
                event_type=event_type,
                event_time=event_time,
                source="action_rule_history",
                history_id=history_id,
                payload={
                    "rule_name": rule_name,
                    "action_result": row.get("actResult"),
                    "level": row.get("level"),
                },
                match_window_seconds=match_window,
                max_active_events=DEFAULT_MAX_ACTIVE_EVENTS,
            )
            created_count += int(created)

        now = str(int(time.time()))
        await self._async_db(store.set_metadata, "history_last_success_epoch", now)
        await self._async_db(store.set_metadata, "history_last_error", "")
        removed = await self._async_db(
            store.cleanup_terminal, DEFAULT_SENT_RETENTION_DAYS
        )
        if created_count:
            self._wake.set()
            _LOGGER.warning(
                "CCTV history reconciliation recovered %d missed event(s)",
                created_count,
            )
        if created_count or removed:
            self._queue_changed()
