"""Daten 2.0 wire contract (spec §J.2).

Everything ABOVE the marker line is mirrored name-for-name and value-for-value by
physical_ai_manager/src/features/editDataset/datenContract.js; the lockstep test
robotis_ai_setup/tests/test_daten_contract_lockstep.py compares the two and the
HTTP_PORT of both nginx /daten-api/ blocks. Regex constants contain no backslash,
so their text is identical in both languages.
"""

SCHEMA_VERSION = 1
HTTP_PORT = 8095
API_PREFIX = '/daten-api/v1'
COMMAND_SERVICE = '/daten/command'
STATE_TOPIC = '/edubotics/daten_state'
TOKEN_TTL_S = 1800
MAX_LINK_DATASETS = 200
ACTIONS = ('link', 'edit', 'delete_dataset', 'upload', 'download', 'keep_both', 'cancel', 'state')
EDIT_OPS = ('delete', 'split', 'merge')
DOWNLOAD_MODES = ('new', 'replace', 'copy')
CANCEL_WHAT = ('upload', 'download')
COMMAND_CODES = ('invalid', 'not_found', 'outside', 'exists', 'stale', 'busy_record', 'busy_upload', 'busy_download', 'busy_edit', 'disk', 'namespace', 'old_format', 'unsupported', 'incomplete', 'in_session', 'unavailable', 'internal')
JOB_OPS = ('delete', 'split', 'merge', 'delete_dataset', 'download', 'keep_both')
JOB_STATES = ('running', 'done', 'failed')
JOB_STAGES = ('prepare', 'download', 'copy', 'verify', 'swap', 'upload')
JOB_UNITS = ('steps', 'bytes')
JOB_FAIL_CODES = ('internal', 'verify_failed', 'layout', 'unaligned', 'unsupported', 'incompatible', 'exists', 'disk', 'not_found', 'auth', 'unreachable', 'old_format', 'other_robot', 'broken', 'token_changed', 'stalled', 'cancelled', 'timeout', 'hub_changed', 'hub_differs', 'unavailable', 'stale')
HTTP_ERRORS = ('invalid', 'token_invalid', 'token_expired', 'scope', 'not_found', 'in_session', 'incomplete', 'unsupported', 'unplayable', 'range', 'overloaded', 'internal')
LOCAL_STATES = ('ok', 'incomplete', 'old_format', 'unsupported', 'in_session')
SYNC_STATES = ('local', 'online', 'current', 'changed', 'newer', 'conflict', 'unknown')
UNKNOWN_REASONS = ('not_asked', 'unreachable', 'not_visible')
BUSY_KINDS = ('record', 'upload', 'download', 'edit', 'delete')
HINT_TYPES = ('idle_start', 'idle_end', 'still', 'no_grasp', 'lag', 'short', 'long')
MERGE_CHECKS = ('version', 'robot', 'fps', 'cameras', 'joints', 'video', 'stats')
HUB_STATES = ('ok', 'skipped', 'no_token', 'token_changed', 'unreachable', 'auth')
PROBE_REFUSALS = ('invalid', 'not_found', 'other_robot', 'old_format', 'unsupported', 'unreachable', 'no_token')
RESERVED_SUFFIXES = ('.tmp_edit', '.bak_edit', '.tmp_sync', '.bak_sync', '.tmp_keep', '.tmp_base', '.trash_edit')
DATASET_PART_RE = '^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$'
REPO_ID_RE = '^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$'
FEATURE_KEY_RE = '^[A-Za-z0-9._-]+$'

# ---- Python-only below this line (not in datenContract.js) ----

CLIP_CACHE_MAX_BYTES = 96 * 2 ** 20
MAX_CONNECTIONS = 32
SOCKET_TIMEOUT_S = 30
MEDIA_WORKERS = 2
MEDIA_QUEUE_MAX = 16
MEDIA_WAIT_S = 20
HUB_WORKERS = 2
HUB_QUEUE_MAX = 8
HUB_WAIT_S = 20
HINT_WORKERS = 1
HUB_CACHE_MAX = 512
HUB_CALL_TIMEOUT_S = 10
EDIT_TIMEOUT_S = 3600
JOB_KEEP_S = 600
JOB_KEEP_MAX = 10
DOWNLOAD_POLL_S = 0.5
DOWNLOAD_STALL_S = 120
DOWNLOAD_TIMEOUT_S = 6 * 3600
START_UPLOAD_POLL_S = 0.5
TAG = 'v3.0'
TAG_RETRIES = 3
READBACK_TRIES = 3
MARKER_PREFIX = '[edubotics:'
SECRET_DIR = '/run/edubotics-daten'
CLIP_TMP_DIR = '/tmp/edubotics-daten'
MAX_INDEX = 10 ** 6
