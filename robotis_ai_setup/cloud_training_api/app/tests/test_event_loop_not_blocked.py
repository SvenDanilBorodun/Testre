"""No route or auth dependency blocks the event loop (API hardening, 2026-11-10).

The API runs ONE uvicorn worker. A synchronous Supabase (or HTTP) call inside an
``async def`` route or dependency stalls every request and every background loop
(the managed-training dispatcher, ``/health``, ``/version``) for its whole round
trip. The rule this file fences:

* a route handler that awaits nothing is a plain ``def`` (FastAPI runs it in its
  threadpool), and
* an ``async def`` route, dependency or helper they await calls every blocking
  function through ``asyncio.to_thread`` (or awaits an async one).

"Blocking" is found by a fixed point over the app's own source: a sync function
that calls ``.execute()`` (supabase-py's network call), ``get_supabase().auth``
or another blocking function. The check is static and by NAME, so it is
conservative: a new blocking helper is caught as soon as it reaches an
``.execute()``.
"""

from __future__ import annotations

import ast
import os
import unittest

APP = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# Modules whose async functions run on the request path.
REQUEST_PATH_MODULES = (
    "auth.py",
    "routes/admin.py",
    "routes/datasets.py",
    "routes/health.py",
    "routes/hf_token.py",
    "routes/inference.py",
    "routes/me.py",
    "routes/policies.py",
    "routes/teacher.py",
    "routes/training.py",
    "routes/training_worker.py",
    "routes/version.py",
    "routes/workflows.py",
    "routes/workgroups.py",
    "services/stop_actions.py",
)

# Calls that never block, whatever the fixed point thinks of their name.
NON_BLOCKING = {"kick", "schedule_publish"}
# Await-free routes that stay ``async def`` ON PURPOSE: they read only the
# environment and the VERSION file, and the probes and the update check must
# never queue behind a busy threadpool.
ASYNC_ON_PURPOSE = {"routes/health.py::health_check", "routes/version.py::get_latest_version"}


def _parse(rel: str) -> ast.Module:
    with open(os.path.join(APP, rel), encoding="utf-8") as fh:
        return ast.parse(fh.read(), filename=rel)


def _all_modules() -> list[str]:
    out = []
    for root, _dirs, files in os.walk(APP):
        if os.sep + "tests" in root or "__pycache__" in root:
            continue
        for f in files:
            if f.endswith(".py"):
                out.append(os.path.relpath(os.path.join(root, f), APP))
    return out


def _callee(call: ast.Call) -> str | None:
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def _own_calls(fn) -> list[ast.Call]:
    """Calls in ``fn``'s own body, not in nested functions or lambdas."""
    out = []
    stack = list(fn.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Call):
            out.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return out


def blocking_names() -> set[str]:
    sync_defs: dict[str, list] = {}
    for rel in _all_modules():
        for node in ast.walk(_parse(rel)):
            if isinstance(node, ast.FunctionDef):
                sync_defs.setdefault(node.name, []).append(node)
    blocking = {"execute", "get_user"}
    changed = True
    while changed:
        changed = False
        for name, defs in sync_defs.items():
            if name in blocking or name in NON_BLOCKING:
                continue
            if any(_callee(c) in blocking for fn in defs for c in _own_calls(fn)):
                blocking.add(name)
                changed = True
    return blocking


def _offloaded(call: ast.Call, parents: dict) -> bool:
    """True when ``call`` sits inside the arguments of ``asyncio.to_thread`` /
    ``run_in_executor`` (it is then built on the loop and run in a thread)."""
    node = call
    while node in parents:
        node = parents[node]
        if isinstance(node, ast.Call) and _callee(node) in ("to_thread", "run_in_executor"):
            return True
    return False


class EventLoopNotBlocked(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.blocking = blocking_names()

    def test_the_fixed_point_finds_the_known_blocking_helpers(self):
        for name in ("get_user_profile", "_read_profile", "credit_quota_row", "record_refusal",
                     "_assert_student_owned", "resolve_visible_workgroup_ids", "_find_recent_duplicate"):
            self.assertIn(name, self.blocking)

    def test_no_async_function_on_the_request_path_calls_a_blocking_function(self):
        offenders = []
        for rel in REQUEST_PATH_MODULES:
            tree = _parse(rel)
            parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
            for fn in ast.walk(tree):
                if not isinstance(fn, ast.AsyncFunctionDef):
                    continue
                for call in _own_calls(fn):
                    name = _callee(call)
                    if name in self.blocking and name not in NON_BLOCKING and not _offloaded(call, parents):
                        offenders.append(f"{rel}::{fn.name} calls {name}() on the event loop (line {call.lineno})")
        self.assertEqual(offenders, [])

    def test_a_route_that_awaits_nothing_is_a_plain_def(self):
        offenders = []
        for rel in REQUEST_PATH_MODULES:
            if not rel.startswith("routes/"):
                continue
            for fn in _parse(rel).body:
                if not isinstance(fn, ast.AsyncFunctionDef):
                    continue
                is_route = any(
                    isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                    and isinstance(d.func.value, ast.Name) and d.func.value.id == "router"
                    for d in fn.decorator_list
                )
                awaits = [n for n in ast.walk(fn) if isinstance(n, (ast.Await, ast.AsyncFor, ast.AsyncWith))]
                if is_route and not awaits and f"{rel}::{fn.name}" not in ASYNC_ON_PURPOSE:
                    offenders.append(f"{rel}::{fn.name}")
        self.assertEqual(offenders, [])

    def test_the_fence_has_teeth(self):
        """The pre-hardening shape of two handlers is refused."""
        src = (
            "async def get_current_admin(user=None):\n"
            "    profile = get_user_profile(str(user.id))\n"
            "    return profile\n"
            "async def create_classroom(req, teacher=None):\n"
            "    supabase.table('classrooms').insert({}).execute()\n"
            "async def fine(user=None):\n"
            "    return await asyncio.to_thread(get_user_profile, str(user.id))\n"
        )
        tree = ast.parse(src)
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        found = sorted(
            (fn.name, _callee(c)) for fn in ast.walk(tree) if isinstance(fn, ast.AsyncFunctionDef)
            for c in _own_calls(fn) if _callee(c) in self.blocking and not _offloaded(c, parents)
        )
        self.assertEqual(found, [("create_classroom", "execute"), ("get_current_admin", "get_user_profile")])

    def test_the_audited_handler_files_have_no_async_route_left(self):
        """admin, teacher (bar the two stop routes, which await the stop),
        workgroups and me (bar the deletion request) were 54 ``async def``
        handlers calling Supabase synchronously before 2026-11-10."""
        allowed = {
            "routes/teacher.py": {"stop_student_training", "student_training_stop_preview"},
            "routes/me.py": {"delete_my_account"},
        }
        for rel in ("routes/admin.py", "routes/teacher.py", "routes/workgroups.py", "routes/me.py"):
            names = {
                fn.name for fn in _parse(rel).body
                if isinstance(fn, ast.AsyncFunctionDef) and any(
                    isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                    and getattr(d.func.value, "id", None) == "router" for d in fn.decorator_list)
            }
            with self.subTest(module=rel):
                self.assertEqual(names, allowed.get(rel, set()))


if __name__ == "__main__":
    unittest.main()
