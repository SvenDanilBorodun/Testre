// Daten 2.0 wire contract (spec §J.2). Mirrors physical_ai_server/daten/contract.py
// above its marker line, name for name and value for value; the lockstep test
// robotis_ai_setup/tests/test_daten_contract_lockstep.py compares the two.
// One `export const` per line; arrays are Object.freeze([...]) of single-quoted strings.

export const SCHEMA_VERSION = 1;
export const HTTP_PORT = 8095;
export const API_PREFIX = '/daten-api/v1';
export const COMMAND_SERVICE = '/daten/command';
export const STATE_TOPIC = '/edubotics/daten_state';
export const TOKEN_TTL_S = 1800;
export const MAX_LINK_DATASETS = 200;
export const ACTIONS = Object.freeze(['link', 'edit', 'delete_dataset', 'upload', 'download', 'keep_both', 'cancel', 'state']);
export const EDIT_OPS = Object.freeze(['delete', 'split', 'merge']);
export const DOWNLOAD_MODES = Object.freeze(['new', 'replace', 'copy']);
export const CANCEL_WHAT = Object.freeze(['upload', 'download']);
export const COMMAND_CODES = Object.freeze(['invalid', 'not_found', 'outside', 'exists', 'stale', 'busy_record', 'busy_upload', 'busy_download', 'busy_edit', 'disk', 'namespace', 'old_format', 'unsupported', 'incomplete', 'in_session', 'unavailable', 'internal']);
export const JOB_OPS = Object.freeze(['delete', 'split', 'merge', 'delete_dataset', 'download', 'keep_both']);
export const JOB_STATES = Object.freeze(['running', 'done', 'failed']);
export const JOB_STAGES = Object.freeze(['prepare', 'download', 'copy', 'verify', 'swap', 'upload']);
export const JOB_UNITS = Object.freeze(['steps', 'bytes']);
export const JOB_FAIL_CODES = Object.freeze(['internal', 'verify_failed', 'layout', 'unaligned', 'unsupported', 'incompatible', 'exists', 'disk', 'not_found', 'auth', 'unreachable', 'old_format', 'other_robot', 'broken', 'token_changed', 'stalled', 'cancelled', 'timeout', 'hub_changed', 'hub_differs', 'unavailable', 'stale']);
export const HTTP_ERRORS = Object.freeze(['invalid', 'token_invalid', 'token_expired', 'scope', 'not_found', 'in_session', 'incomplete', 'unsupported', 'unplayable', 'range', 'overloaded', 'internal']);
export const LOCAL_STATES = Object.freeze(['ok', 'incomplete', 'old_format', 'unsupported', 'in_session']);
export const SYNC_STATES = Object.freeze(['local', 'online', 'current', 'changed', 'newer', 'conflict', 'unknown']);
export const UNKNOWN_REASONS = Object.freeze(['not_asked', 'unreachable', 'not_visible']);
export const BUSY_KINDS = Object.freeze(['record', 'upload', 'download', 'edit', 'delete']);
export const HINT_TYPES = Object.freeze(['idle_start', 'idle_end', 'still', 'no_grasp', 'lag', 'short', 'long']);
export const MERGE_CHECKS = Object.freeze(['version', 'robot', 'fps', 'cameras', 'joints', 'video', 'stats']);
export const HUB_STATES = Object.freeze(['ok', 'skipped', 'no_token', 'token_changed', 'unreachable', 'auth']);
export const PROBE_REFUSALS = Object.freeze(['invalid', 'not_found', 'other_robot', 'old_format', 'unsupported', 'unreachable', 'no_token']);
export const RESERVED_SUFFIXES = Object.freeze(['.tmp_edit', '.bak_edit', '.tmp_sync', '.bak_sync', '.tmp_keep', '.tmp_base', '.trash_edit']);
export const DATASET_PART_RE = '^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$';
export const REPO_ID_RE = '^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$';
export const FEATURE_KEY_RE = '^[A-Za-z0-9._-]+$';
