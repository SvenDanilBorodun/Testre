# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The platform token's own Hugging Face account, a reserved namespace (SA-08).

No student may claim the account behind the platform ``HF_TOKEN`` as their
Hugging Face namespace: it can read (and write) repositories no student owns.
Its name is never taken from a client. It is resolved here:

  * at boot and then every ``RETRY_S`` until it succeeds, by the Hub's whoami of
    ``HF_TOKEN`` (``training_dispatch._hf_whoami_body``, bounded), and again on
    every successful managed-training HF write check (``note``);
  * from migration 059's ``public.hf_reserved_namespaces`` table, which holds the
    organisation and every platform account a previous boot recorded, so a boot
    whose whoami fails still knows the name.

``hf_identity.note_platform_account`` holds the names for this process; this
module writes each newly learned one to the table (service role), where 059's
triggers refuse it as ``users.hf_username`` and as a ``datasets`` namespace.
Everything runs off the event loop and is best effort: a database without 059
(or a Hub that does not answer) leaves the in-process deny-list in force and is
logged, never raised.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading

from app.services import hf_identity

logger = logging.getLogger(__name__)

TABLE = "hf_reserved_namespaces"
RETRY_S = 300            # until the whoami succeeded once
REFRESH_S = 6 * 3600     # afterwards (a rotated token may name another account)

_pending: set[str] = set()      # learned in-process, not yet written to the table
_pending_lock = threading.Lock()
_resolved = False


def note(name) -> None:
    """Reserve ``name`` (the platform token's whoami name) now; persist it later."""
    if hf_identity.note_platform_account(name):
        with _pending_lock:
            _pending.add(name.strip().lower())
        logger.info("platform HF account recorded as a reserved namespace")


def _load_table(supabase) -> None:
    rows = supabase.table(TABLE).select("name_lower, kind").execute().data or []
    for row in rows:
        if row.get("kind") == "platform_account":
            hf_identity.note_platform_account(row.get("name_lower"))


def _persist(supabase) -> None:
    with _pending_lock:
        names = sorted(_pending)
    for name in names:
        supabase.table(TABLE).upsert({"name_lower": name, "kind": "platform_account"},
                                      on_conflict="name_lower").execute()
        with _pending_lock:
            _pending.discard(name)


def sync_once() -> bool:
    """BLOCKING: load the table, ask whoami, write what is new. True once the
    platform account was resolved by whoami in this process."""
    global _resolved
    from app.services.supabase_client import get_supabase

    supabase = None
    try:
        supabase = get_supabase()
        _load_table(supabase)
    except Exception as exc:  # noqa: BLE001 — a database without 059 keeps the in-process list
        logger.warning("platform identity: reserved namespaces not readable (%s)", type(exc).__name__)

    token = (os.environ.get("HF_TOKEN") or "").strip()
    if token:
        from app.services import hf_credentials
        from app.services.training_dispatch import _hf_whoami_body

        try:
            note(_hf_whoami_body(token).get("name"))
            _resolved = True
        except hf_credentials.HfCredentialError as exc:
            logger.warning("platform identity: whoami of HF_TOKEN failed (%s); retrying later", exc.code)

    if supabase is not None:
        try:
            _persist(supabase)
        except Exception as exc:  # noqa: BLE001
            logger.warning("platform identity: could not record the platform account (%s)", type(exc).__name__)
    return _resolved


async def platform_identity_loop() -> None:
    """Startup task: resolve and record the platform account, off the event loop."""
    while True:
        try:
            resolved = await asyncio.to_thread(sync_once)
        except Exception as exc:  # noqa: BLE001 — the loop never dies
            logger.error("platform identity: tick raised %s", type(exc).__name__)
            resolved = False
        await asyncio.sleep(REFRESH_S if resolved else RETRY_S)
