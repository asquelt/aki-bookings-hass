"""Constants."""
from __future__ import annotations

DOMAIN = "aki_bookings"
API_PATH = "/api/ha/v1"

CONF_URL = "url"
CONF_TOKEN = "token"
CONF_SITE_TOKEN = "site_access_token"
CONF_LOCK = "lock_entity"

OPT_SCAN_INTERVAL = "scan_interval"
OPT_SLOT_LIMIT = "slot_limit"
OPT_DRY_RUN = "dry_run"
OPT_DRY_RUN_REPORT = "dry_run_report"
OPT_REC_ENABLED = "recordings_enabled"
OPT_GUARD = "guard_entity"
OPT_S3_ENDPOINT = "s3_endpoint"
OPT_S3_BUCKET = "s3_bucket"
OPT_S3_KEY = "s3_access_key"
OPT_S3_SECRET = "s3_secret_key"
OPT_S3_ROOT = "s3_root"
OPT_DOWNLOAD_DAYS = "download_days"
OPT_DELETE_SOURCES = "delete_sources"  # after a verified upload, remove the files from the cards and the console disk

DEFAULT_URL = "https://pero.ro"
DEFAULT_SCAN_INTERVAL = 300  # s
DEFAULT_SLOT_LIMIT = 8  # of the lock's 10 Matter users; the rest stay free for hand-made codes
DEFAULT_GUARD = "input_boolean.tryb_nagrywania"  # on = a take is being recorded; uploads must stay away
DEFAULT_S3_ROOT = "nagrania"
DEFAULT_DOWNLOAD_DAYS = 7  # also the longest a signed link can live
DEFAULT_DRY_RUN = True  # a fresh install touches neither the lock nor the booking system

WINDOW_BACK_H = 3  # the feed filters by session overlap; valid_to can trail the session by up to 2 h
WINDOW_AHEAD_D = 14

RETRY_DELAYS_S = (30, 60, 120)  # after these the code is in the Error state; later tries follow the poll
LOCK_CALL_TIMEOUT_S = 90
STALE_SYNC_S = 30 * 60
STARTUP_DELAY_S = 60  # let Matter come up before the first pass after a restart

EVENT_CODE_APPLIED = f"{DOMAIN}_code_applied"
EVENT_CODE_REMOVED = f"{DOMAIN}_code_removed"
EVENT_CODE_FAILED = f"{DOMAIN}_code_failed"
EVENT_SYNC_FAILED = f"{DOMAIN}_sync_failed"
EVENT_REC_READY = f"{DOMAIN}_recordings_ready"
EVENT_REC_FAILED = f"{DOMAIN}_recordings_failed"
EVENT_REC_EMPTY = f"{DOMAIN}_recordings_empty"
EVENT_REC_LEFT = f"{DOMAIN}_recordings_left"
EVENT_REC_NONE = f"{DOMAIN}_recordings_none"
EVENT_REC_UNASSIGNED = f"{DOMAIN}_recordings_unassigned"
EVENT_REC_DELETED = f"{DOMAIN}_recordings_deleted"
EVENT_SRC_DELETED = f"{DOMAIN}_recordings_sources_deleted"
EVENT_SRC_FAILED = f"{DOMAIN}_recordings_sources_failed"

SERVICE_SYNC_NOW = "sync_now"
SERVICE_RECONCILE_LOCK = "reconcile_lock"
SERVICE_REVOKE_CODE = "revoke_code"
SERVICE_SET_RECORDING_OPT_IN = "set_recording_opt_in"
SERVICE_REPORT_RECORDING = "report_recording"
SERVICE_RESOLVE_RECORDINGS = "resolve_recordings"
SERVICE_RUN_RECORDINGS = "run_recordings"

STATE_PENDING = "pending"
STATE_APPLIED = "applied"
STATE_FAILED = "failed"
