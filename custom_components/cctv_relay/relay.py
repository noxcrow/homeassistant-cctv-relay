"""Durable queue and Synology helpers for the HAOS-native CCTV Relay."""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import json
import logging
import pathlib
import re
import shutil
import sqlite3
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from typing import Any, Iterator


LOG = logging.getLogger(__name__)
MAX_JSON_RESPONSE_BYTES = 2 * 1024 * 1024


class ConfigError(RuntimeError):
    """Configuration is absent or unsafe."""


class RelayError(RuntimeError):
    """A retryable external operation failed."""

    def __init__(self, message: str, *, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class SynologyAPIError(RelayError):
    def __init__(self, code: int, operation: str):
        super().__init__(f"Synology {operation} failed with API code {code}")
        self.code = code
        self.operation = operation


@dataclasses.dataclass(frozen=True)
class CameraConfig:
    key: str
    camera_id: int
    slot_name: str

    @property
    def display_name(self) -> str:
        return f"{self.slot_name} (ID {self.camera_id})"

    @property
    def caption(self) -> str:
        return f"{self.display_name}에서 움직임을 감지했습니다."

    @property
    def lost_message(self) -> str:
        return f"{self.display_name} 연결이 끊어졌습니다."

    @property
    def restored_message(self) -> str:
        return f"{self.display_name}가 재연결되었습니다."


class EventStore:
    """Thread-safe durable queue backed by SQLite WAL."""

    def __init__(self, path: str):
        db_path = pathlib.Path(path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            db_path, timeout=30, check_same_thread=False, isolation_level=None
        )
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA busy_timeout=30000")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_key TEXT NOT NULL UNIQUE,
                    camera_key TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    event_time REAL NOT NULL,
                    source TEXT NOT NULL,
                    history_id INTEGER UNIQUE,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL,
                    claimed_at REAL,
                    last_error TEXT,
                    telegram_message_id INTEGER,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    sent_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_events_due
                    ON events(camera_key, status, next_attempt, event_time);
                CREATE INDEX IF NOT EXISTS idx_events_match
                    ON events(camera_key, event_type, event_time);
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )
            now = time.time()
            self._connection.execute(
                """
                UPDATE events
                   SET status='retry', next_attempt=?, claimed_at=NULL,
                       last_error='recovered after relay restart', updated_at=?
                 WHERE status='processing'
                """,
                (now, now),
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")

    def enqueue(
        self,
        *,
        event_key: str,
        camera_key: str,
        event_type: str,
        event_time: float,
        source: str,
        payload: dict[str, Any] | None = None,
        history_id: int | None = None,
        match_window_seconds: int = 0,
    ) -> tuple[int, bool]:
        now = time.time()
        payload_json = json.dumps(
            payload or {}, ensure_ascii=False, separators=(",", ":")
        )
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT id FROM events WHERE event_key=?", (event_key,)
            ).fetchone()
            if row:
                LOG.debug("Deduplicated CCTV event by exact key: %s -> %s", event_key, row["id"])
                return int(row["id"]), False
            if history_id is not None:
                row = connection.execute(
                    "SELECT id FROM events WHERE history_id=?", (history_id,)
                ).fetchone()
                if row:
                    LOG.debug("Deduplicated CCTV history id %s -> event %s", history_id, row["id"])
                    return int(row["id"]), False
            if match_window_seconds > 0:
                # Surveillance Station can emit repeated motion triggers for one
                # physical movement. Treat nearby motion events for the same
                # camera as one burst regardless of source. For non-motion
                # events, fuzzy matching remains cross-source only.
                is_motion = event_type == "motion"
                source_clause = "" if is_motion else "AND source<>?"
                history_clause = (
                    "" if is_motion else "AND (? IS NULL OR history_id IS NULL)"
                )
                params: list[Any] = [camera_key, event_type]
                if not is_motion:
                    params.extend([source, history_id])
                params.extend(
                    [
                        event_time - match_window_seconds,
                        event_time + match_window_seconds,
                        event_time,
                    ]
                )
                row = connection.execute(
                    f"""
                    SELECT id, history_id, event_time, source
                      FROM events
                     WHERE camera_key=? AND event_type=?
                       {source_clause}
                       {history_clause}
                       AND event_time BETWEEN ? AND ?
                     ORDER BY ABS(event_time - ?), id
                     LIMIT 1
                    """,
                    tuple(params),
                ).fetchone()
                if row:
                    if history_id is not None and row["history_id"] is None:
                        connection.execute(
                            """
                            UPDATE events
                               SET history_id=?, updated_at=?
                             WHERE id=? AND history_id IS NULL
                            """,
                            (history_id, now, int(row["id"])),
                        )
                    delta = abs(event_time - float(row["event_time"]))
                    if event_type == "motion":
                        LOG.info(
                            "Coalesced CCTV motion burst: %s/%s -> event %s "
                            "(existing_source=%s, delta=%.3fs)",
                            camera_key,
                            source,
                            row["id"],
                            row["source"],
                            delta,
                        )
                    else:
                        LOG.info(
                            "Correlated CCTV %s event with existing %s event %s "
                            "(delta=%.3fs)",
                            source,
                            "history" if source == "webhook" else "webhook",
                            row["id"],
                            delta,
                        )
                    return int(row["id"]), False
            cursor = connection.execute(
                """
                INSERT INTO events (
                    event_key, camera_key, event_type, event_time, source,
                    history_id, payload_json, status, attempts, next_attempt,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)
                """,
                (
                    event_key,
                    camera_key,
                    event_type,
                    event_time,
                    source,
                    history_id,
                    payload_json,
                    now,
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid), True

    def claim_next(self, camera_key: str) -> dict[str, Any] | None:
        now = time.time()
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM events
                 WHERE camera_key=? AND status IN ('pending', 'retry')
                   AND next_attempt <= ?
                 ORDER BY event_time, id
                 LIMIT 1
                """,
                (camera_key, now),
            ).fetchone()
            if row is None:
                return None
            updated = connection.execute(
                """
                UPDATE events
                   SET status='processing', claimed_at=?, updated_at=?
                 WHERE id=? AND status IN ('pending', 'retry')
                """,
                (now, now, int(row["id"])),
            )
            if updated.rowcount != 1:
                return None
            return dict(row)

    def mark_sent(self, event_id: int, telegram_message_id: int | None) -> None:
        now = time.time()
        with self._lock:
            self._connection.execute(
                """
                UPDATE events
                   SET status='sent', telegram_message_id=?, sent_at=?,
                       claimed_at=NULL, last_error=NULL, updated_at=?
                 WHERE id=?
                """,
                (telegram_message_id, now, now, event_id),
            )

    def mark_retry(self, event_id: int, error: str, next_attempt: float) -> None:
        safe_error = error.replace("\n", " ")[:1000]
        now = time.time()
        with self._lock:
            self._connection.execute(
                """
                UPDATE events
                   SET status='retry', attempts=attempts+1, next_attempt=?,
                       claimed_at=NULL, last_error=?, updated_at=?
                 WHERE id=?
                """,
                (next_attempt, safe_error, now, event_id),
            )

    def set_metadata(self, key: str, value: str) -> None:
        now = time.time()
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO metadata(key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                    updated_at=excluded.updated_at
                """,
                (key, value[:2000], now),
            )

    def summary(self) -> dict[str, Any]:
        with self._lock:
            counts = {
                str(row["status"]): int(row["count"])
                for row in self._connection.execute(
                    "SELECT status, COUNT(*) AS count FROM events GROUP BY status"
                ).fetchall()
            }
            oldest = self._connection.execute(
                """
                SELECT MIN(event_time) AS oldest
                  FROM events WHERE status IN ('pending', 'retry', 'processing')
                """
            ).fetchone()["oldest"]
            metadata = {
                str(row["key"]): str(row["value"])
                for row in self._connection.execute(
                    "SELECT key, value FROM metadata"
                ).fetchall()
            }
        return {
            "counts": counts,
            "oldest_unsent_epoch": oldest,
            "metadata": metadata,
        }

    def cleanup_sent(self, retention_days: int) -> int:
        cutoff = time.time() - retention_days * 86400
        with self._lock:
            cursor = self._connection.execute(
                "DELETE FROM events WHERE status='sent' AND sent_at < ?", (cutoff,)
            )
            return int(cursor.rowcount)


@dataclasses.dataclass(frozen=True)
class SynologySession:
    sid: str
    synotoken: str | None


class SynologyClient:
    REQUIRED_APIS = (
        "SYNO.API.Auth",
        "SYNO.SurveillanceStation.Recording",
        "SYNO.SurveillanceStation.ActionRule",
    )

    def __init__(
        self,
        base_url: str,
        ca_file: str | None,
        timeout_seconds: int,
        export_timeout_seconds: int,
        verify_ssl: bool = True,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.export_timeout_seconds = export_timeout_seconds
        self.ssl_context = ssl.create_default_context(cafile=ca_file)
        if not verify_ssl:
            self.ssl_context.check_hostname = False
            self.ssl_context.verify_mode = ssl.CERT_NONE
        self._api_info: dict[str, dict[str, Any]] | None = None
        self._discovery_lock = threading.Lock()

    def _build_url(self, path: str, params: dict[str, Any]) -> str:
        encoded = urllib.parse.urlencode(params)
        return f"{self.base_url}/webapi/{path.lstrip('/')}?{encoded}"

    def _request_json(
        self,
        path: str,
        params: dict[str, Any],
        *,
        post: bool = False,
    ) -> dict[str, Any]:
        if post:
            url = f"{self.base_url}/webapi/{path.lstrip('/')}"
            data = urllib.parse.urlencode(params).encode("utf-8")
            request = urllib.request.Request(
                url,
                data=data,
                method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        else:
            request = urllib.request.Request(
                self._build_url(path, params), method="GET"
            )
        try:
            with urllib.request.urlopen(
                request, context=self.ssl_context, timeout=self.timeout_seconds
            ) as response:
                body = response.read(MAX_JSON_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise RelayError(f"Synology HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            detail = (
                exc.reason
                if isinstance(exc, urllib.error.URLError)
                else type(exc).__name__
            )
            raise RelayError(
                f"Synology connection failed: {detail}"
            ) from exc
        if len(body) > MAX_JSON_RESPONSE_BYTES:
            raise RelayError("Synology JSON response exceeded safety limit")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RelayError("Synology returned an invalid JSON response") from exc
        if not payload.get("success"):
            code = int(payload.get("error", {}).get("code", 100))
            operation = str(params.get("method", params.get("api", "request")))
            raise SynologyAPIError(code, operation)
        data = payload.get("data", {})
        return data if isinstance(data, dict) else {"value": data}

    def discover(self) -> dict[str, dict[str, Any]]:
        with self._discovery_lock:
            if self._api_info is not None:
                return self._api_info
            query = ",".join(self.REQUIRED_APIS)
            data = self._request_json(
                "query.cgi",
                {
                    "api": "SYNO.API.Info",
                    "version": 1,
                    "method": "query",
                    "query": query,
                },
            )
            missing = [api for api in self.REQUIRED_APIS if api not in data]
            if missing:
                raise ConfigError(
                    f"Synology is missing required APIs: {', '.join(missing)}"
                )
            self._api_info = {str(key): value for key, value in data.items()}
            return self._api_info

    def _api_path(self, api: str) -> str:
        info = self.discover()[api]
        return str(info["path"])

    def _api_version(self, api: str, requested: int) -> int:
        info = self.discover()[api]
        maximum = int(info["maxVersion"])
        minimum = int(info["minVersion"])
        version = min(requested, maximum)
        if version < minimum:
            raise ConfigError(
                f"Synology API {api} does not support version {requested}"
            )
        return version

    def login(self, username: str, password: str) -> SynologySession:
        api = "SYNO.API.Auth"
        data = self._request_json(
            self._api_path(api),
            {
                "api": api,
                "version": self._api_version(api, 6),
                "method": "login",
                "account": username,
                "passwd": password,
                "session": "SurveillanceStation",
                "format": "sid",
                "enable_syno_token": "yes",
            },
            post=True,
        )
        sid = data.get("sid")
        if not isinstance(sid, str) or not sid:
            raise RelayError("Synology login returned no session ID")
        token = data.get("synotoken")
        return SynologySession(sid=sid, synotoken=str(token) if token else None)

    def logout(self, session: SynologySession) -> None:
        api = "SYNO.API.Auth"
        params: dict[str, Any] = {
            "api": api,
            "version": self._api_version(api, 6),
            "method": "logout",
            "session": "SurveillanceStation",
            "_sid": session.sid,
        }
        if session.synotoken:
            params["SynoToken"] = session.synotoken
        self._request_json(self._api_path(api), params, post=True)

    @contextlib.contextmanager
    def session(self, username: str, password: str) -> Iterator[SynologySession]:
        active = self.login(username, password)
        try:
            yield active
        finally:
            try:
                self.logout(active)
            except RelayError as exc:
                LOG.warning("Synology logout failed: %s", exc)

    def call_json(
        self,
        api: str,
        method: str,
        requested_version: int,
        session: SynologySession,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request_params: dict[str, Any] = {
            "api": api,
            "version": self._api_version(api, requested_version),
            "method": method,
            "_sid": session.sid,
        }
        if session.synotoken:
            request_params["SynoToken"] = session.synotoken
        if params:
            request_params.update(params)
        return self._request_json(self._api_path(api), request_params)

    def export_range(
        self,
        *,
        username: str,
        password: str,
        camera_id: int,
        from_time: int,
        to_time: int,
        file_basename: str,
        destination_directory: str,
    ) -> pathlib.Path:
        api = "SYNO.SurveillanceStation.Recording"
        with self.session(username, password) as active:
            started = self.call_json(
                api,
                "RangeExport",
                6,
                active,
                {
                    "camId": camera_id,
                    "fromTime": from_time,
                    "toTime": to_time,
                    "fileName": file_basename,
                },
            )
            try:
                download_id = int(started["dlid"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RelayError(
                    "Synology RangeExport returned no download ID"
                ) from exc

            deadline = time.monotonic() + self.export_timeout_seconds
            extension = ""
            while time.monotonic() < deadline:
                progress_data = self.call_json(
                    api,
                    "GetRangeExportProgress",
                    6,
                    active,
                    {"dlid": download_id},
                )
                progress = int(progress_data.get("progress", -1))
                if progress == -1:
                    raise RelayError("Synology range export task failed")
                if progress >= 100:
                    extension = str(progress_data.get("fileExt", "")).lower()
                    break
                time.sleep(1)
            else:
                raise RelayError("Synology range export timed out")
            if extension not in {"mp4", "zip"}:
                raise RelayError(
                    "Synology returned unsupported export type: "
                    f"{extension or 'empty'}"
                )

            output_path = (
                pathlib.Path(destination_directory)
                / f"{file_basename}.{extension}"
            )
            request_params: dict[str, Any] = {
                "api": api,
                "version": self._api_version(api, 6),
                "method": "OnRangeExportDone",
                "dlid": download_id,
                "fileName": file_basename,
                "_sid": active.sid,
            }
            if active.synotoken:
                request_params["SynoToken"] = active.synotoken
            request = urllib.request.Request(
                self._build_url(self._api_path(api), request_params), method="GET"
            )
            try:
                with urllib.request.urlopen(
                    request,
                    context=self.ssl_context,
                    timeout=self.export_timeout_seconds,
                ) as response, output_path.open("wb") as output:
                    total = 0
                    first = response.read(64 * 1024)
                    if first.lstrip().startswith(b"{"):
                        try:
                            payload = json.loads(first.decode("utf-8"))
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            payload = None
                        if isinstance(payload, dict) and not payload.get(
                            "success", True
                        ):
                            code = int(payload.get("error", {}).get("code", 100))
                            raise SynologyAPIError(code, "OnRangeExportDone")
                    output.write(first)
                    total += len(first)
                    while True:
                        chunk = response.read(64 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
                        total += len(chunk)
                        if total > 256 * 1024 * 1024:
                            raise RelayError(
                                "Synology export exceeded hard safety limit"
                            )
            except urllib.error.HTTPError as exc:
                raise RelayError(
                    f"Synology export download returned HTTP {exc.code}"
                ) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                detail = (
                    exc.reason
                    if isinstance(exc, urllib.error.URLError)
                    else type(exc).__name__
                )
                raise RelayError(f"Synology export download failed: {detail}") from exc
            if output_path.stat().st_size == 0:
                raise RelayError("Synology exported an empty file")
            return output_path

    def list_history(
        self, username: str, password: str, page_size: int
    ) -> list[dict[str, Any]]:
        api = "SYNO.SurveillanceStation.ActionRule"
        with self.session(username, password) as active:
            first = self.call_json(
                api,
                "ListHistory",
                1,
                active,
                {"start": 0, "limit": page_size},
            )
            rows = list(first.get("history", []))
            total = int(first.get("total", len(rows)))
            if total > page_size:
                last = self.call_json(
                    api,
                    "ListHistory",
                    1,
                    active,
                    {"start": max(0, total - page_size), "limit": page_size},
                )
                rows.extend(last.get("history", []))
        unique: dict[int, dict[str, Any]] = {}
        for row in rows:
            if isinstance(row, dict) and "id" in row:
                unique[int(row["id"])] = row
        return list(unique.values())

    def list_recordings(
        self,
        username: str,
        password: str,
        camera_id: int,
        from_time: int,
        to_time: int,
    ) -> list[dict[str, Any]]:
        api = "SYNO.SurveillanceStation.Recording"
        with self.session(username, password) as active:
            data = self.call_json(
                api,
                "List",
                6,
                active,
                {
                    "offset": 0,
                    "limit": 1,
                    "cameraIds": str(camera_id),
                    "fromTime": from_time,
                    "toTime": to_time,
                },
            )
        recordings = data.get("recordings", [])
        return recordings if isinstance(recordings, list) else []
def _safe_zip_members(
    archive: zipfile.ZipFile, destination: pathlib.Path
) -> list[pathlib.Path]:
    extracted: list[pathlib.Path] = []
    total_uncompressed = 0
    members = sorted(archive.infolist(), key=lambda item: item.filename)
    for index, info in enumerate(members):
        if info.is_dir() or not info.filename.lower().endswith(".mp4"):
            continue
        total_uncompressed += int(info.file_size)
        if total_uncompressed > 256 * 1024 * 1024:
            raise RelayError("Synology ZIP export exceeded safety limit")
        target = destination / f"part_{index:03d}.mp4"
        with archive.open(info, "r") as source, target.open("wb") as output:
            shutil.copyfileobj(source, output, length=64 * 1024)
        extracted.append(target)
    if not extracted:
        raise RelayError("Synology ZIP export contained no MP4 files")
    return extracted


_DURATION_RE = re.compile(
    r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", re.IGNORECASE
)
MIN_VIDEO_DURATION_SECONDS = 0.5


def _parse_ffmpeg_duration(output: str) -> float | None:
    """Extract the container duration reported by ffmpeg."""
    match = _DURATION_RE.search(output)
    if match is None:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _validate_video_duration(video: pathlib.Path, ffmpeg_path: str) -> None:
    """Reject videos whose container reports no usable playback duration."""
    try:
        result = subprocess.run(
            [ffmpeg_path, "-nostdin", "-hide_banner", "-i", str(video)],
            check=False,
            timeout=30,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise RelayError("ffmpeg could not inspect the exported video") from exc
    duration = _parse_ffmpeg_duration(result.stderr or "")
    if duration is None:
        raise RelayError("exported video has no readable duration metadata")
    if duration < MIN_VIDEO_DURATION_SECONDS:
        raise RelayError(
            f"exported video duration {duration:.3f}s is too short; retrying after recording index settles"
        )
    LOG.info("Validated CCTV clip %s: duration=%.3fs size=%d", video.name, duration, video.stat().st_size)


def _remux_mp4(
    input_path: pathlib.Path, working_directory: pathlib.Path, ffmpeg_path: str
) -> pathlib.Path:
    """Rewrite MP4 timestamps/metadata so Telegram receives a seekable clip."""
    output = working_directory / f"normalized_{input_path.stem}.mp4"
    command = [
        ffmpeg_path,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-fflags",
        "+genpts",
        "-i",
        str(input_path),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c",
        "copy",
        "-avoid_negative_ts",
        "make_zero",
        "-movflags",
        "+faststart",
        "-y",
        str(output),
    ]
    try:
        subprocess.run(
            command,
            check=True,
            timeout=90,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        raise RelayError("ffmpeg could not normalize the Synology MP4 export") from exc
    if not output.is_file() or output.stat().st_size == 0:
        raise RelayError("ffmpeg produced an empty normalized video")
    _validate_video_duration(output, ffmpeg_path)
    return output


def normalize_export(
    exported_path: pathlib.Path, working_directory: pathlib.Path, ffmpeg_path: str
) -> pathlib.Path:
    if exported_path.suffix.lower() == ".mp4":
        return _remux_mp4(exported_path, working_directory, ffmpeg_path)
    if exported_path.suffix.lower() != ".zip":
        raise RelayError(f"unsupported exported file: {exported_path.suffix}")
    try:
        with zipfile.ZipFile(exported_path) as archive:
            parts = _safe_zip_members(archive, working_directory)
    except (zipfile.BadZipFile, OSError) as exc:
        raise RelayError("Synology returned an invalid ZIP export") from exc
    if len(parts) == 1:
        return _remux_mp4(parts[0], working_directory, ffmpeg_path)

    concat_file = working_directory / "concat.txt"
    concat_file.write_text(
        "".join(f"file '{part.name}'\n" for part in parts), encoding="utf-8"
    )
    output = working_directory / "merged.mp4"
    copy_command = [
        ffmpeg_path,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        concat_file.name,
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        output.name,
    ]
    try:
        subprocess.run(
            copy_command,
            cwd=working_directory,
            check=True,
            timeout=90,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        transcode_command = [
            ffmpeg_path,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            concat_file.name,
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "24",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            output.name,
        ]
        try:
            subprocess.run(
                transcode_command,
                cwd=working_directory,
                check=True,
                timeout=180,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            OSError,
        ) as exc:
            raise RelayError("ffmpeg could not merge the Synology ZIP export") from exc
    if not output.is_file() or output.stat().st_size == 0:
        raise RelayError("ffmpeg produced an empty video")
    _validate_video_duration(output, ffmpeg_path)
    return output


def parse_event_time(value: Any) -> float:
    if value is None or value == "":
        return time.time()
    if isinstance(value, (int, float)):
        timestamp = float(value)
    else:
        text = str(value).strip()
        try:
            timestamp = float(text)
        except ValueError:
            try:
                timestamp = dt.datetime.fromisoformat(
                    text.replace("Z", "+00:00")
                ).timestamp()
            except ValueError as exc:
                raise ValueError("event_time must be Unix epoch or ISO-8601") from exc
    if timestamp > 10_000_000_000:
        timestamp /= 1000.0
    if timestamp < 946684800 or timestamp > time.time() + 300:
        raise ValueError("event_time is outside the accepted range")
    return timestamp
