# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The one shape of a new Supabase Auth account the server creates.

Migration 058 refuses every new auth.users row whose app_metadata does not
carry ``{"edubotics_provisioned": true}`` (a deferred check at COMMIT, because
Supabase Auth writes app_metadata with an UPDATE after its INSERT). Only the
service role can set app_metadata (``auth.admin.create_user``), never a
browser's ``signUp``, so the marker tells an account the server created from a
self-registration. Every server path that creates an account builds its
attributes here: ``routes/teacher.py::create_student`` and
``routes/admin.py::create_teacher``; ``scripts/bootstrap_admin.py`` (outside
the app package) spells the same literal, held equal by
``tests/test_supabase_058.py``.
"""

from __future__ import annotations

PROVISIONED_KEY = "edubotics_provisioned"


def new_account_attributes(email: str, password: str) -> dict:
    """``auth.admin.create_user``'s attributes for an account the server creates."""
    return {
        "email": email,
        "password": password,
        "email_confirm": True,
        "app_metadata": {PROVISIONED_KEY: True},
    }
