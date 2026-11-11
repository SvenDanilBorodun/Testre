"""SA-08 (2026-11-13): the platform HF_TOKEN never acts outside the organisation.

The platform token belongs to the account that publishes every trained model
into the platform's organisation (``hf_identity.HF_ORG``). It can read and
delete what that account can, which reaches beyond the students' data. So:

  * every call on a STUDENT namespace (dataset sync, the sweep, dataset
    registration) is anonymous (``token=False``), and
  * every call that carries the platform token names a repo in the
    organisation (the deletion of a student's own published model, the
    teacher's checkpoint list of an organisation model, the publish).

Two halves: a behavioural one, which drives each Hub-touching path with a
recording ``HfApi`` and a forged ``users.hf_username`` equal to the
organisation, and a structural one, which lists every place in the app that
builds an ``HfApi`` / ``hf_hub_download`` without ``token=False`` and pins each
to the guard that keeps it inside the organisation.
"""

from __future__ import annotations

import ast
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.tests.test_dataset_identity_routes import _ensure_stubs
from app.tests.test_gdpr_erasure_honesty import (
    ORG, _cred, _d, _Supabase, _t,
)

PLATFORM = "hf_platform_secret_for_tests"
APP = Path(__file__).resolve().parents[1]


class _Recorder:
    """One recording HfApi class per test; every call lands in ``calls`` as
    ``(token, method, repo_or_author)``."""

    def __init__(self, listing=None, files=None):
        self.calls = []
        self.listing = listing or {}
        self.files = files or []
        rec = self

        class _HfApi:
            def __init__(self, *a, token="<unset>", **k):
                self._token = token

            def _log(self, method, target):
                rec.calls.append((self._token, method, target))

            def list_datasets(self, author=None, **k):
                self._log("list_datasets", author)
                return rec.listing.get(author, [])

            def dataset_info(self, repo_id, **k):
                self._log("dataset_info", repo_id)
                return SimpleNamespace(author=repo_id.split("/", 1)[0])

            def delete_repo(self, repo_id=None, repo_type=None, missing_ok=False, **k):
                self._log("delete_repo", repo_id)

            def list_repo_files(self, repo_id, repo_type=None, **k):
                self._log("list_repo_files", repo_id)
                return rec.files

        self.HfApi = _HfApi

    def platform_calls_outside_the_org(self):
        return [c for c in self.calls
                if c[0] == PLATFORM
                and (not isinstance(c[2], str) or c[2].split("/", 1)[0].lower() != ORG.lower())]


class TheBehaviouralHalf(unittest.TestCase):
    def setUp(self):
        _ensure_stubs()
        from app.routes import datasets as dsroute
        from app.routes import teacher
        self.ds = dsroute
        self.teacher = teacher
        self.env = patch.dict(os.environ, {"HF_TOKEN": PLATFORM, "DATASET_SWEEP_ENABLED": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def _assert_scoped(self, rec):
        self.assertTrue(rec.calls, "the path made no Hub call — the test is vacuous")
        self.assertEqual(rec.platform_calls_outside_the_org(), [], rec.calls)

    def test_sync_with_a_forged_org_name_and_a_real_proven_name(self):
        from app.tests.test_dataset_identity_routes import _SyncSupabase
        rec = _Recorder(listing={"anna": [SimpleNamespace(id="anna/d1")]})
        sb = _SyncSupabase([], "anna")
        with patch.object(self.ds, "get_user_profile",
                          return_value={"id": "u1", "hf_username": ORG, "workgroup_id": None}), \
                patch.object(self.ds, "get_supabase", return_value=sb), \
                patch.object(self.ds, "HfApi", rec.HfApi):
            self.ds.sync_datasets(user=SimpleNamespace(id="u1"))
        self._assert_scoped(rec)
        self.assertEqual({c[0] for c in rec.calls}, {False})

    def test_register_reads_the_author_anonymously(self):
        rec = _Recorder()

        class _RpcSb:
            def rpc(self, fn, params):
                return SimpleNamespace(execute=lambda: SimpleNamespace(data={
                    "id": "d", "owner_user_id": "u1", "hf_repo_id": params["p_hf_repo_id"],
                    "name": "x", "description": "", "workgroup_id": None,
                    "created_at": "t", "updated_at": "t"}))

        payload = SimpleNamespace(hf_repo_id="anna/wuerfel", name="x", description="",
                                  episode_count=None, total_frames=None, fps=None, robot_type=None)
        with patch.object(self.ds, "get_user_profile", return_value={"id": "u1", "workgroup_id": None}), \
                patch.object(self.ds, "get_supabase", return_value=_RpcSb()), \
                patch.object(self.ds, "HfApi", rec.HfApi):
            self.ds.register_dataset(payload, user=SimpleNamespace(id="u1"))
        self._assert_scoped(rec)
        self.assertEqual({c[0] for c in rec.calls}, {False})

    def test_register_of_an_org_repo_makes_no_hub_call(self):
        rec = _Recorder()
        payload = SimpleNamespace(hf_repo_id=f"{ORG}/private-org-data", name="x", description="",
                                  episode_count=None, total_frames=None, fps=None, robot_type=None)
        with patch.object(self.ds, "HfApi", rec.HfApi):
            with self.assertRaises(Exception) as caught:
                self.ds.register_dataset(payload, user=SimpleNamespace(id="u1"))
        self.assertEqual(getattr(caught.exception, "status_code", None), 403)
        self.assertEqual(rec.calls, [])

    def test_the_sweep_is_anonymous(self):
        from app.services import dataset_sweep as ds
        import app.services.supabase_client  # noqa: F401
        from app.tests.test_workgroups import _SweepSupabase
        rec = _Recorder(listing={"anna": [SimpleNamespace(id="anna/d1")]})
        sb = _SweepSupabase(users=[{"id": "u1", "hf_username": "anna", "workgroup_id": None},
                                   {"id": "u2", "hf_username": ORG, "workgroup_id": None}])
        with patch("app.services.supabase_client.get_supabase", return_value=sb), \
                patch.object(sys.modules["huggingface_hub"], "HfApi", rec.HfApi):
            ds._run_sweep_once()
        self._assert_scoped(rec)
        self.assertEqual({c[0] for c in rec.calls}, {False})
        self.assertNotIn(ORG, [c[2] for c in rec.calls])

    def test_deletion_with_a_forged_org_name_deletes_only_the_own_model(self):
        rec = _Recorder()
        store = {
            "trainings": [_t(f"{ORG}/act-own", f"{ORG}/org-ds"), _t("anna/legacy-model"),
                          _t(f"{ORG}/act-sibling"), _t(f"{ORG}/act-sibling", user="stu2")],
            "datasets": [_d(f"{ORG}/private-org-data"), _d("anna/rec")],
            "user_hf_credentials": [_cred(ORG)],
        }
        sb = _Supabase(store)
        with patch.object(self.teacher, "_assert_student_owned", lambda *a: {}), \
                patch.object(self.teacher, "get_supabase", lambda: sb), \
                patch.object(self.teacher, "HfApi", rec.HfApi):
            self.teacher.delete_student("stu1", teacher={"id": "t1"})
        self._assert_scoped(rec)
        self.assertEqual(rec.calls, [(PLATFORM, "delete_repo", f"{ORG}/act-own")])

    def _checkpoints(self, model_name):
        rec = _Recorder(files=["checkpoints/000100/pretrained_model/config.json"])

        class _CkptSb:
            def table(self, _name):
                q = SimpleNamespace()
                q.select = lambda *a, **k: q
                q.eq = lambda *a, **k: q
                q.limit = lambda *a, **k: q
                q.execute = lambda: SimpleNamespace(
                    data=[{"id": 7, "model_name": model_name, "user_id": "stu1"}])
                return q

        self.teacher._ckpt_cache.clear()
        with patch.object(self.teacher, "_assert_student_owned", lambda *a: {}), \
                patch.object(self.teacher, "get_supabase", lambda: _CkptSb()), \
                patch.object(self.teacher, "HfApi", rec.HfApi):
            self.teacher.list_training_checkpoints("stu1", 7, teacher={"id": "t1"})
        return rec

    def test_checkpoint_list_of_an_org_model_uses_the_platform_token(self):
        rec = self._checkpoints(f"{ORG}/act-own")
        self._assert_scoped(rec)
        self.assertEqual(rec.calls[0][0], PLATFORM)

    def test_checkpoint_list_of_any_other_model_is_anonymous(self):
        rec = self._checkpoints("anna/legacy-model")
        self._assert_scoped(rec)
        self.assertEqual(rec.calls[0][0], False)


# ---------------------------------------------------------------------------
# The structural half
# ---------------------------------------------------------------------------

# Every Hub client in the app that is NOT built with token=False, and why its
# repo is in the organisation. A new one fails until it is added here with its
# guard (and the guard has a test).
ALLOWED_TOKENED_HUB_CALLS = {
    ("routes/teacher.py", "_delete_student_hf_artifacts"):
        "deletes only an organisation MODEL (is_org_repo belt; test_gdpr_erasure_honesty)",
    ("routes/teacher.py", "list_training_checkpoints"):
        "platform token only when is_org_repo(model_name), else token=False",
    ("services/hf_publish.py", "_hf"):
        "publishes into trainings.model_name, generated by routes/training.py::_generate_model_name in HF_ORG",
    ("services/dataset_preflight.py", "_preflight_dataset"):
        "token is the caller's argument; every caller passes the literal False (checked below)",
    ("services/dataset_preflight.py", "_download_worker"):
        "nested in _preflight_dataset: the same caller's token, always False",
}

_HUB_CALLEES = {"HfApi", "hf_hub_download", "snapshot_download"}


def _callee_name(call: ast.Call) -> str | None:
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def _tokened_hub_calls():
    found = set()
    for path in sorted(APP.rglob("*.py")):
        rel = path.relative_to(APP).as_posix()
        if rel.startswith("tests/"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call) or _callee_name(node) not in _HUB_CALLEES:
                    continue
                token = next((k.value for k in node.keywords if k.arg == "token"), None)
                if isinstance(token, ast.Constant) and token.value is False:
                    continue
                found.add((rel, fn.name))
    return found


class TheStructuralHalf(unittest.TestCase):
    def test_every_tokened_hub_client_is_accounted_for(self):
        found = _tokened_hub_calls()
        self.assertEqual(found - set(ALLOWED_TOKENED_HUB_CALLS), set(),
                         "a Hub client without token=False that nothing proves stays in the organisation")
        self.assertEqual(set(ALLOWED_TOKENED_HUB_CALLS) - found, set(),
                         "a stale entry: that function no longer builds a tokened Hub client")

    def test_every_preflight_caller_passes_False(self):
        calls = []
        for path in sorted(APP.rglob("*.py")):
            rel = path.relative_to(APP).as_posix()
            if rel.startswith("tests/"):
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Call) and _callee_name(node) == "preflight_dataset" \
                        and not rel.endswith("dataset_preflight.py"):
                    calls.append((rel, node))
        self.assertTrue(calls)
        for rel, node in calls:
            token = node.args[1] if len(node.args) > 1 else next(
                (k.value for k in node.keywords if k.arg == "token"), None)
            self.assertTrue(isinstance(token, ast.Constant) and token.value is False,
                            f"{rel}: preflight_dataset must read the dataset anonymously")

    def test_generated_model_names_are_in_the_organisation(self):
        """The publish's repo is trainings.model_name, which only
        routes/training.py::_generate_model_name creates: it must start with
        the ONE organisation constant."""
        tree = ast.parse((APP / "routes" / "training.py").read_text(encoding="utf-8"))
        imports = [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
                   and n.module == "app.services.hf_identity"
                   and any(a.name == "HF_ORG" for a in n.names)]
        self.assertTrue(imports, "training.py must take HF_ORG from hf_identity")
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_generate_model_name")
        returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
        self.assertEqual(len(returns), 1)
        left = returns[0].value.left
        self.assertIsInstance(left, ast.JoinedStr)
        self.assertEqual(ast.unparse(left), "f'{HF_ORG}/'")

    def test_cloud_inference_reads_only_organisation_models(self):
        src = (APP / "services" / "inference_sessions.py").read_text(encoding="utf-8")
        self.assertIn(f'MODEL_REPO_RE = re.compile(r"{ORG}/', src)


if __name__ == "__main__":
    unittest.main()
