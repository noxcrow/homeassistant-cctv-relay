"""Constants for the CCTV Relay integration."""

from homeassistant.const import Platform

DOMAIN = "cctv_relay"
PLATFORMS = (Platform.SENSOR,)

CONF_SYNOLOGY_URL = "synology_url"
CONF_VERIFY_SSL = "verify_ssl"
CONF_FRONT_CAMERA_ID = "front_camera_id"
CONF_BACK_CAMERA_ID = "back_camera_id"
CONF_TELEGRAM_NOTIFY_ENTITY = "telegram_notify_entity"
CONF_WEBHOOK_ID = "webhook_id"
CONF_PRE_SECONDS = "pre_seconds"
CONF_POST_SECONDS = "post_seconds"
CONF_HISTORY_ENABLED = "history_enabled"
CONF_HISTORY_INTERVAL = "history_interval"
CONF_HISTORY_LOOKBACK = "history_lookback"
CONF_HISTORY_PAGE_SIZE = "history_page_size"
CONF_MATCH_WINDOW = "match_window"
CONF_MAX_CLIP_MEGABYTES = "max_clip_megabytes"
CONF_FFMPEG_PATH = "ffmpeg_path"


DEFAULT_PRE_SECONDS = 3
DEFAULT_POST_SECONDS = 5
MAX_PRE_SECONDS = 15
MAX_POST_SECONDS = 30
MAX_CLIP_SECONDS = 30
DEFAULT_VERIFY_SSL = True
DEFAULT_HISTORY_ENABLED = True
DEFAULT_HISTORY_INTERVAL = 60
DEFAULT_HISTORY_LOOKBACK = 172800
DEFAULT_HISTORY_PAGE_SIZE = 200
DEFAULT_MATCH_WINDOW = 15
DEFAULT_MAX_CLIP_MEGABYTES = 45
DEFAULT_FFMPEG_PATH = "/usr/bin/ffmpeg"
DEFAULT_INDEX_GRACE_SECONDS = 1
DEFAULT_RETRY_BASE_SECONDS = 1.0
DEFAULT_RETRY_MAX_SECONDS = 300.0
DEFAULT_SENT_RETENTION_DAYS = 30
DEFAULT_DEDUP_WINDOW_SECONDS = 3
DEFAULT_DSM_TIMEOUT_SECONDS = 20
DEFAULT_EXPORT_TIMEOUT_SECONDS = 120

CAMERA_DEFINITIONS = {
    "front": {
        "slot_name": "카메라 1",
    },
    "back": {
        "slot_name": "카메라 2",
    },
}

RULE_MAP = {
    # Generic rule names recommended for new installations.
    "TG_CAMERA1_MOTION": ("front", "motion"),
    "TG_CAMERA2_MOTION": ("back", "motion"),
    "TG_CAMERA1_LOST": ("front", "lost"),
    "TG_CAMERA1_RESTORED": ("front", "restored"),
    "TG_CAMERA2_LOST": ("back", "lost"),
    "TG_CAMERA2_RESTORED": ("back", "restored"),
    # Legacy names retained for existing installations.
    "TG_FRONT_MOTION": ("front", "motion"),
    "TG_BACK_MOTION": ("back", "motion"),
    "TG_FRONT_LOST": ("front", "lost"),
    "TG_FRONT_RESTORED": ("front", "restored"),
    "TG_BACK_LOST": ("back", "lost"),
    "TG_BACK_RESTORED": ("back", "restored"),
}


ALLOWED_EVENT_TYPES = frozenset({"motion", "lost", "restored", "test"})
SIGNAL_QUEUE_UPDATED = f"{DOMAIN}_queue_updated"
