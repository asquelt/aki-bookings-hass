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

DEFAULT_URL = "https://pero.ro"
DEFAULT_SCAN_INTERVAL = 300  # s
DEFAULT_SLOT_LIMIT = 8  # of the lock's 10 Matter users; the rest stay free for hand-made codes
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

SERVICE_SYNC_NOW = "sync_now"
SERVICE_RECONCILE_LOCK = "reconcile_lock"
SERVICE_REVOKE_CODE = "revoke_code"
SERVICE_SET_RECORDING_OPT_IN = "set_recording_opt_in"
SERVICE_REPORT_RECORDING = "report_recording"

STATE_PENDING = "pending"
STATE_APPLIED = "applied"
STATE_FAILED = "failed"
