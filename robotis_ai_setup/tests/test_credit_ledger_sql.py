"""Migration 052 (the training-hour ledger) and its rollback, read as text.

The behaviour is proven on a local stack by
``supabase/tests/052_credit_ledger_assertions.sql`` (the PASS count its header
states); what is pinned here, deps-free and in CI, is the shape a careless edit
would break (spec §12.3, §20.2):

* every function is written ``CREATE OR REPLACE FUNCTION public.<name>(``;
* every callable credit function is SECURITY DEFINER with
  ``SET search_path = public`` (review B1), and stop_training, the changed
  049/050 functions and the credit-column guard stay SECURITY INVOKER;
* the ledger is append-only (both immutability triggers) and holds no free-text
  column (a reason is a ``credit_reasons`` row referenced by id, no FK);
* every REVOKE/GRANT: the four tables, their sequences, trainings, the column
  grant, and one EXECUTE loop that names every function 052 defines; the
  in-migration self-check names every one too;
* the P-code set (P0060–P0073 all raised, nothing outside the known set);
* fix round 2 (2026-11-01): mark_checkpoint_ready is the only writer of a
  ready checkpoint and decides under the trainings row lock, claim_checkpoint_mirror
  is 049's text plus one status condition, the waiver needs current_step 0, and
  the rollback drops the first and restores 049's mirror claim (md5 checked);
* fix round 2026-11-01: finish_training raises P0073 for an unproven charge
  waiver before its first write, and update_training_progress is 049's body
  with exactly the stall-clock line changed (same signature and grants, and
  the rollback restores 049's body and grants);
* the two review-1 m4 traps are absent: no OUT column of a RETURNS TABLE
  function is read unqualified, and the compatibility functions keep their
  exact old return types;
* the SQL interface implementer B builds against (§20.1): the four re-created
  signatures and the 13 new RPCs' argument names;
* the rollback refuses first, drops the seven triggers and the four re-created
  signatures, restores each replaced body byte for byte (after whitespace
  normalisation) from the last pre-052 definition, and keeps the ledger.
"""

from __future__ import annotations

import pathlib
import re
import unittest

_SUPABASE = pathlib.Path(__file__).resolve().parents[1] / "supabase"
_MIG = _SUPABASE / "migrations"
M051 = _MIG / "20261025120000_051_trajectory_robot_profile_v1.sql"
M052 = _MIG / "20261028120000_052_credit_ledger.sql"
R052 = _SUPABASE / "rollback" / "20261028120000_052_credit_ledger_rollback.sql"
A052 = _SUPABASE / "tests" / "052_credit_ledger_assertions.sql"
PRE052 = [_MIG / "00000000000000_baseline.sql"] + sorted(
    p for p in _MIG.glob("2026*.sql") if "_052_" not in p.name)

# spec §4 / §5: who is what
INTERNAL_HELPERS = {
    "_credit_post", "_credit_request_replay", "_credit_reason", "_credit_child_account", "_credit_return_free",
    "_credit_lock_user", "_credit_startable", "_credit_open_seconds", "_credit_meta", "_credit_settle", "_credit_close",
}
TRIGGER_FUNCTIONS = {
    "credit_ledger_immutable", "trainings_guard_credit_columns", "trainings_credit_settle",
    "trainings_credit_settle_delete", "users_credit_close", "workgroups_credit_close", "credit_reparent_on_move",
}
DEFINER_CALLABLES = {
    "start_training_safe", "start_continuation_safe", "credit_admin_grant", "credit_admin_revoke",
    "credit_admin_compensate", "credit_assign", "credit_reclaim", "credit_set_member_limit", "credit_quota",
    "credit_history", "credit_erase_reason", "record_training_refusal", "credit_reconcile", "credit_teacher_summary",
    "get_remaining_credits", "get_teacher_credit_summary", "adjust_student_credits", "adjust_workgroup_credits",
}
INVOKER_CALLABLES = {
    "stop_training", "cap_training_attempt", "requeue_training", "finish_training", "cancel_request_training",
    "complete_cancel_training", "claim_training_publish", "mark_checkpoint_ready", "claim_checkpoint_mirror",
}
# the worker's own RPC: 049's body with one line changed, EXECUTE for anon/authenticated (not in the loop)
WORKER_RPC = {"update_training_progress"}
UTP_SIG = "INT, UUID, TEXT, INT, INT, REAL, TEXT, TEXT"
UTP_049_LINE = "      WHEN step_advanced_at IS NULL OR p_status = 'running' THEN NOW()\n"
UTP_052_LINE = "      WHEN step_advanced_at IS NULL OR (p_status = 'running' AND status = 'queued') THEN NOW()\n"
COMPAT = ("get_remaining_credits", "get_teacher_credit_summary", "adjust_student_credits", "adjust_workgroup_credits")
# spec §19 step 3: the bodies the rollback restores (the last pre-052 definition of each)
RESTORED = {
    "start_training_safe": "UUID, TEXT, TEXT, TEXT, JSONB, INT, UUID, BOOLEAN",
    "start_continuation_safe": "UUID, INT, TEXT",
    "cancel_request_training": "INT, UUID",
    "claim_training_publish": "INT, INT, INT, TEXT",
    "cap_training_attempt": "INT, INT, INT, TEXT, TEXT, INT",
    "requeue_training": "INT, INT, TEXT, TEXT, INT, INT",
    "finish_training": "INT, INT, TEXT, TEXT, TEXT, TEXT, INT, TIMESTAMPTZ",
    "complete_cancel_training": "INT, TEXT, TEXT",
    "claim_checkpoint_mirror": "INT, INT, INT, INT, TEXT",
    "get_remaining_credits": "UUID",
    "get_teacher_credit_summary": "UUID",
    "adjust_student_credits": "UUID, UUID, INTEGER",
    "adjust_workgroup_credits": "UUID, UUID, INTEGER",
}
SEVEN_TRIGGERS = (
    ("trg_0_trainings_credit_guard", "trainings"), ("trg_9_trainings_credit_settle", "trainings"),
    ("trg_9_trainings_credit_settle_delete", "trainings"), ("trg_9_users_credit_close", "users"),
    ("trg_9_workgroups_credit_close", "workgroups"), ("trg_9_users_credit_reparent", "users"),
    ("trg_9_workgroups_credit_reparent", "workgroups"),
)
NEW_P_CODES = {f"P00{n}" for n in range(60, 74)}
KNOWN_P_CODES = NEW_P_CODES | {"P0001", "P0002", "P0003", "P0023", "P0049", "P0050", "P0051", "P0052", "P0053", "P0054"}


def _read(p) -> str:
    return p.read_text(encoding="utf-8")


def _code(sql: str) -> str:
    """The SQL without `--` comments (the files carry no `--` inside a string)."""
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _definitions(sql: str) -> list:
    """(name, header, body, full) for every CREATE OR REPLACE FUNCTION public.<name>( … $tag$;"""
    out = []
    for m in re.finditer(r"CREATE OR REPLACE FUNCTION public\.([A-Za-z_0-9]+)\(", sql):
        q = re.compile(r"\$([A-Za-z_]*)\$").search(sql, m.end())
        tag = q.group(0)
        close = sql.index(tag, q.end())
        semi = sql.index(";", close + len(tag))
        out.append((m.group(1), sql[m.start():q.start()], sql[q.end():close], sql[m.start():semi + 1]))
    return out


def _defs_by_name(sql: str) -> dict:
    out = {}
    for name, header, body, full in _definitions(sql):
        out.setdefault(name, []).append((header, body, full))
    return out


def _last_pre052(name: str) -> str:
    best = None
    for f in PRE052:
        for n, _h, _b, full in _definitions(_read(f)):
            if n == name:
                best = full
    return best


def _paren(text: str, start: int) -> str:
    """The text inside the parenthesis that opens at text[start]."""
    assert text[start] == "("
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[start + 1:i]
    raise AssertionError("unbalanced parenthesis")


def _args(header: str) -> str:
    return _paren(header, header.index("("))


def _arg_names(header: str) -> list:
    return [a.split()[0] for a in _norm(_args(header)).split(",") if a.strip()]


def _returns_table(header: str):
    m = re.search(r"RETURNS TABLE\s*\(", header)
    return None if m is None else _paren(header, m.end() - 1)


def _out_names(header: str) -> list:
    rt = _returns_table(header)
    return [] if rt is None else [c.split()[0] for c in _norm(rt).split(",") if c.strip()]


def _strip_literals(body: str) -> str:
    body = re.sub(r"--[^\n]*", "", body)
    return re.sub(r"'(?:[^']|'')*'", "''", body)


def _unqualified_out_uses(header: str, body: str) -> list:
    """OUT columns of a RETURNS TABLE function read unqualified in its body (review 1 m4).

    An INSERT target column list is not an expression (plpgsql never substitutes
    there), so it is cut out before the search; a name preceded by '.' or a word
    character (an alias, a prefix) is qualified or another identifier.
    """
    code = _strip_literals(body)
    code = re.sub(r"INSERT INTO\s+[\w.]+\s*\([^)]*\)", "INSERT INTO x()", code)
    hits = []
    for name in _out_names(header):
        for m in re.finditer(r"(?<![.\w])" + re.escape(name) + r"\b", code):
            hits.append((name, code[max(0, m.start() - 40):m.end() + 20].replace("\n", " ")))
    return hits


def _type_norm(cols: str) -> list:
    out = []
    for c in _norm(cols).lower().split(","):
        name, typ = c.split(None, 1)
        out.append((name, {"int": "integer", "int4": "integer", "int8": "bigint"}.get(typ.strip(), typ.strip())))
    return out


def _array(block: str, name: str) -> set:
    m = re.search(name + r"\s+TEXT\[\]\s*:=\s*ARRAY\[(.*?)\];", block, re.S)
    assert m, name
    return set(re.findall(r"'([a-z_0-9]+)'", m.group(1)))


class FilesAndOrder(unittest.TestCase):
    def test_every_file_exists(self):
        for p in (M052, R052, A052):
            self.assertTrue(p.is_file(), p)

    def test_052_sorts_after_051(self):
        self.assertGreater(M052.name, M051.name)
        # 052 comes right after 051; a later migration (053, the Servo-Station's) may follow it.
        names = sorted(p.name for p in _MIG.glob("*.sql"))
        self.assertEqual(names.index(M052.name), names.index(M051.name) + 1)

    def test_one_transaction_each(self):
        for p in (M052, R052):
            code = _code(_read(p))
            self.assertEqual(code.count("BEGIN;"), 1, p.name)
            self.assertTrue(code.rstrip().endswith("COMMIT;"), p.name)

    def test_every_function_is_create_or_replace_public(self):
        code = _code(_read(M052))
        self.assertNotRegex(code, r"CREATE\s+FUNCTION")
        self.assertEqual(len(re.findall(r"CREATE OR REPLACE FUNCTION", code)),
                         len(re.findall(r"CREATE OR REPLACE FUNCTION public\.", code)))
        names = [n for n, *_ in _definitions(code)]
        self.assertEqual(len(names), len(set(names)), "a function is defined twice")


class SecurityModel(unittest.TestCase):
    def setUp(self):
        self.defs = _defs_by_name(_code(_read(M052)))

    def test_the_function_set_is_exactly_the_spec(self):
        self.assertEqual(set(self.defs),
                         INTERNAL_HELPERS | TRIGGER_FUNCTIONS | DEFINER_CALLABLES | INVOKER_CALLABLES | WORKER_RPC)

    def test_every_callable_credit_function_is_definer_with_search_path(self):
        for name in DEFINER_CALLABLES | INTERNAL_HELPERS:
            ((header, _b, _f),) = self.defs[name]
            self.assertIn("SECURITY DEFINER", header, name)
            self.assertIn("SET search_path = public", header, name)
            self.assertNotIn("SECURITY INVOKER", header, name)

    def test_the_049_pattern_functions_and_the_guard_stay_invoker(self):
        for name in INVOKER_CALLABLES | {"trainings_guard_credit_columns"}:
            ((header, _b, _f),) = self.defs[name]
            self.assertIn("SECURITY INVOKER", header, name)
            self.assertNotIn("SECURITY DEFINER", header, name)

    def test_the_writing_trigger_functions_are_definer(self):
        for name in TRIGGER_FUNCTIONS - {"credit_ledger_immutable", "trainings_guard_credit_columns"}:
            ((header, _b, _f),) = self.defs[name]
            self.assertIn("RETURNS trigger", header, name)
            self.assertIn("SECURITY DEFINER", header, name)
            self.assertIn("SET search_path = public", header, name)

    def test_the_guard_covers_the_six_credit_columns_and_three_request_roles(self):
        ((_h, body, _f),) = self.defs["trainings_guard_credit_columns"]
        self.assertIn("current_user IN ('service_role','authenticated','anon')", body)
        for col in ("credit_scheme", "reserved_hours", "charged_hours", "settled_at", "idempotency_key", "user_id"):
            self.assertIn(f"NEW.{col}", body, col)
            self.assertIn(f"OLD.{col}", body, col)
        self.assertIn("ERRCODE = '42501'", body)


class LedgerShape(unittest.TestCase):
    def setUp(self):
        self.code = _code(_read(M052))
        start = self.code.index("CREATE TABLE public.credit_ledger (")
        self.table = _paren(self.code, start + len("CREATE TABLE public.credit_ledger "))

    def test_the_ledger_is_append_only_even_for_its_owner(self):
        ((_h, body, _f),) = _defs_by_name(self.code)["credit_ledger_immutable"]
        self.assertIn("ERRCODE = 'P0068'", body)
        self.assertIn("CREATE TRIGGER credit_ledger_no_change BEFORE UPDATE OR DELETE ON public.credit_ledger\n"
                      "  FOR EACH ROW EXECUTE FUNCTION public.credit_ledger_immutable();", self.code)
        self.assertIn("CREATE TRIGGER credit_ledger_no_truncate BEFORE TRUNCATE ON public.credit_ledger\n"
                      "  FOR EACH STATEMENT EXECUTE FUNCTION public.credit_ledger_immutable();", self.code)

    def test_no_free_text_column_in_the_ledger(self):
        cols = []
        for line in self.table.split("\n"):
            line = line.strip()
            if not line or line.startswith(("CONSTRAINT", "OR ", "(", ")")) or not re.match(r"[a-z_]+ [A-Z]", line):
                continue
            for part in re.split(r",\s*(?=[a-z_]+ [A-Z])", line):
                name, typ = part.split()[:2]
                cols.append((name, typ.rstrip(",")))
        self.assertEqual([c for c, _ in cols], [
            "id", "at", "kind", "hours", "from_kind", "from_id", "from_bucket", "to_kind", "to_id", "to_bucket",
            "training_id", "member_id", "actor_id", "actor_role", "request_id", "reason_id", "meta"])
        text_cols = {c for c, t in cols if t == "TEXT"}
        # the coded columns only: two with an IN-list CHECK, four fixed by the shape CHECK's literals
        self.assertEqual(text_cols, {"kind", "from_kind", "from_bucket", "to_kind", "to_bucket", "actor_role"})
        self.assertRegex(self.table, r"kind TEXT NOT NULL CHECK \(kind IN \(")
        self.assertRegex(self.table, r"actor_role TEXT NOT NULL CHECK \(actor_role IN \('admin','teacher','student','system'\)\)")
        shape = self.table[self.table.index("CONSTRAINT credit_ledger_shape"):]
        for col in ("from_bucket", "to_bucket"):
            for m in re.finditer(col + r"\s*(=|IS)\s*(\S+)", shape):
                self.assertIn(m.group(2).rstrip(")"), ("'free'", "'reserved'", "NULL"), (col, m.group(0)))
        # the reason is a number: no FK (the reason row is erasable), and no text of it here
        self.assertRegex(self.table, r"reason_id BIGINT,")
        self.assertNotRegex(self.table, r"reason_id BIGINT[^,]*REFERENCES")
        self.assertNotRegex(self.table, r"\b(reason|note|comment|text|description|message) TEXT\b")

    def test_the_four_one_per_training_indexes(self):
        for kind in ("reserve", "charge", "release"):
            self.assertIn(f"CREATE UNIQUE INDEX credit_ledger_one_{kind}", self.code)
        self.assertIn("CREATE UNIQUE INDEX credit_ledger_request ON public.credit_ledger (request_id) "
                      "WHERE request_id IS NOT NULL;", self.code)

    def test_free_text_lives_only_in_the_erasable_reasons_table(self):
        self.assertIn("text TEXT NOT NULL CHECK (char_length(text) BETWEEN 1 AND 200)", self.code)
        self.assertNotRegex(self.code, r"CREATE TRIGGER \w+ BEFORE [A-Z ]+ ON public\.credit_reasons")


class Grants(unittest.TestCase):
    def setUp(self):
        self.code = _code(_read(M052))
        self.names = {n for n, *_ in _definitions(self.code)} - WORKER_RPC

    def test_the_four_tables_are_service_role_read_only(self):
        tabs = "public.credit_accounts, public.credit_ledger, public.credit_reasons, public.training_start_refusals"
        self.assertIn(f"REVOKE ALL ON {tabs}\n  FROM PUBLIC, anon, authenticated, service_role;", self.code)
        self.assertIn(f"GRANT SELECT ON {tabs}\n  TO service_role;", self.code)
        for t in tabs.split(", "):
            self.assertIn(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;", self.code)
        self.assertNotRegex(self.code, r"CREATE POLICY")
        self.assertNotRegex(self.code, r"ALTER PUBLICATION")
        self.assertIn("EXECUTE format('REVOKE ALL ON SEQUENCE %s FROM PUBLIC, anon, authenticated, service_role',", self.code)

    def test_trainings_privileges(self):
        self.assertIn("REVOKE INSERT ON public.trainings FROM service_role;", self.code)
        self.assertIn("REVOKE TRUNCATE, TRIGGER, REFERENCES ON public.trainings FROM service_role;", self.code)
        grants = re.findall(r"GRANT SELECT \(([^)]*)\)\s*ON public\.trainings TO anon, authenticated;", self.code)
        self.assertEqual(grants, ["reserved_hours, charged_hours, stop_requested_by_role, lost_seconds"])

    def test_one_execute_loop_names_every_function(self):
        end = self.code.index("EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC, anon, authenticated', r.sig);")
        loop = self.code[self.code.rindex("DO $$", 0, end):end]
        internals, callables = _array(loop, "internals"), _array(loop, "callables")
        self.assertEqual(internals, INTERNAL_HELPERS | TRIGGER_FUNCTIONS)
        self.assertEqual(callables, DEFINER_CALLABLES | INVOKER_CALLABLES)
        self.assertEqual(internals | callables, self.names)
        self.assertIn("EXECUTE format('REVOKE ALL ON FUNCTION %s FROM service_role', r.sig);", self.code)
        self.assertIn("EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO service_role', r.sig);", self.code)

    def test_the_self_check_names_every_function_and_runs_last(self):
        tail = self.code[self.code.rindex("DO $$"):]
        self.assertIn("RAISE NOTICE '052 self-check passed';", tail)
        self.assertEqual(_array(tail, "internals") | _array(tail, "callables"), self.names)
        self.assertEqual(_array(tail, "invokers"), INVOKER_CALLABLES | {"trainings_guard_credit_columns"})
        for needle in ("has_table_privilege", "has_sequence_privilege", "has_function_privilege",
                       "has_column_privilege", "pg_publication_tables", "prosecdef", "'search_path=public'",
                       "credit_reconcile()", "tgtype & 2", "'stop_requested_by','time_limit_s'", "'host_key'"):
            self.assertIn(needle, tail, needle)
        self.assertLess(self.code.rindex("DO $$"), self.code.rindex("COMMIT;"))

    def test_the_nine_triggers(self):
        for name, table in SEVEN_TRIGGERS:
            self.assertRegex(self.code, rf"CREATE TRIGGER {name} (BEFORE|AFTER) [A-Z ]+(OF \w+ )?ON public\.{table}")
        for name in ("trg_0_trainings_credit_guard", "trg_9_trainings_credit_settle", "trg_9_trainings_credit_settle_delete"):
            self.assertRegex(self.code, rf"CREATE TRIGGER {name} BEFORE")
        for name in ("trg_9_users_credit_close", "trg_9_workgroups_credit_close", "trg_9_users_credit_reparent",
                     "trg_9_workgroups_credit_reparent"):
            self.assertRegex(self.code, rf"CREATE TRIGGER {name} AFTER")


class PCodes(unittest.TestCase):
    def test_the_p_code_set(self):
        codes = set(re.findall(r"ERRCODE = '(P\d{4})'", _code(_read(M052))))
        self.assertEqual(NEW_P_CODES - codes, set(), "a new P-code of spec §6 is never raised")
        self.assertEqual(codes - KNOWN_P_CODES, set(), "a P-code outside spec §6")


class M4Traps(unittest.TestCase):
    def test_no_out_column_is_read_unqualified(self):
        hits = []
        for name, header, body, _full in _definitions(_code(_read(M052))):
            hits += [(name,) + h for h in _unqualified_out_uses(header, body)]
        self.assertEqual(hits, [])

    def test_the_checker_has_teeth(self):
        header = "CREATE OR REPLACE FUNCTION public.f(p INT) RETURNS TABLE(training_id INT, reserved_hours INT) AS "
        self.assertEqual([n for n, _ in _unqualified_out_uses(header, "SELECT reserved_hours FROM trainings")],
                         ["reserved_hours"])
        self.assertEqual(_unqualified_out_uses(header, "SELECT t.reserved_hours FROM trainings t; -- reserved_hours"), [])
        self.assertEqual(_unqualified_out_uses(header, "INSERT INTO trainings(reserved_hours) VALUES (1)"), [])

    def test_the_compat_functions_keep_their_exact_old_return_types(self):
        defs = _defs_by_name(_code(_read(M052)))
        for name in COMPAT:
            ((header, _b, _f),) = defs[name]
            old = _last_pre052(name)
            self.assertIsNotNone(old, name)
            self.assertEqual(_type_norm(_returns_table(header)), _type_norm(_returns_table(old)), name)
            self.assertEqual(_arg_names(header), _arg_names(old), name)
        self.assertEqual(_type_norm(_returns_table(defs["get_remaining_credits"][0][0])),
                         [("training_credits", "integer"), ("trainings_used", "bigint"), ("remaining", "bigint")])


class InterfaceForTheApi(unittest.TestCase):
    """§20.1 A -> B: the signatures and argument names the cloud API calls."""

    def setUp(self):
        self.defs = _defs_by_name(_code(_read(M052)))

    def _sig(self, name):
        ((header, _b, _f),) = self.defs[name]
        return _norm(_args(header)), _norm(_returns_table(header) or "")

    def test_the_four_re_created_signatures(self):
        self.assertEqual(self._sig("start_training_safe"), (
            "p_user_id UUID, p_dataset_name TEXT, p_model_name TEXT, p_model_type TEXT, p_training_params JSONB, "
            "p_total_steps INT, p_worker_token UUID, p_managed BOOLEAN DEFAULT false, p_idempotency_key UUID DEFAULT NULL, "
            "p_max_hours INT DEFAULT 3", "training_id INT, remaining INT, reserved_hours INT, replay BOOLEAN"))
        self.assertEqual(self._sig("start_continuation_safe"), (
            "p_user_id UUID, p_parent_id INT, p_note TEXT, p_max_hours INT DEFAULT 3",
            "o_training_id INT, o_remaining INT, o_reserved_hours INT"))
        self.assertEqual(self._sig("finish_training")[0],
                         "p_training_id INT, p_attempt INT, p_status TEXT, p_message TEXT, p_log_url TEXT, "
                         "p_note TEXT DEFAULT NULL, p_final_step INT DEFAULT NULL, p_continuable_until TIMESTAMPTZ DEFAULT NULL, "
                         "p_charge_waiver TEXT DEFAULT NULL")
        self.assertEqual(self._sig("cancel_request_training")[0],
                         "p_training_id INT, p_user_id UUID, p_actor_role TEXT DEFAULT NULL")
        code = _code(_read(M052))
        for drop in ("DROP FUNCTION public.start_training_safe(UUID, TEXT, TEXT, TEXT, JSONB, INT, UUID, BOOLEAN);",
                     "DROP FUNCTION public.start_continuation_safe(UUID, INT, TEXT);",
                     "DROP FUNCTION public.finish_training(INT, INT, TEXT, TEXT, TEXT, TEXT, INT, TIMESTAMPTZ);",
                     "DROP FUNCTION public.cancel_request_training(INT, UUID);"):
            self.assertIn(drop, code)

    def test_the_new_rpcs_argument_names_match_the_schema_probe(self):
        expected = {  # spec §12.1's probe bodies
            "credit_quota": ["p_user_id", "p_max_hours"],
            "credit_admin_grant": ["p_admin_id", "p_teacher_id", "p_hours", "p_reason", "p_request_id"],
            "credit_admin_revoke": ["p_admin_id", "p_teacher_id", "p_hours", "p_reason", "p_request_id"],
            "credit_assign": ["p_teacher_id", "p_target_kind", "p_target_id", "p_hours", "p_request_id"],
            "credit_reclaim": ["p_teacher_id", "p_target_kind", "p_target_id", "p_hours", "p_request_id"],
            "credit_set_member_limit": ["p_teacher_id", "p_group_id", "p_hours"],
            "credit_teacher_summary": ["p_teacher_id"],
            "credit_history": ["p_scope", "p_id", "p_before", "p_limit"],
            "stop_training": ["p_training_id", "p_user_id", "p_actor_id", "p_actor_role", "p_note"],
            "record_training_refusal": ["p_user_id", "p_request_kind", "p_code", "p_model_type"],
            "credit_reconcile": [],
            "credit_admin_compensate": ["p_admin_id", "p_target_kind", "p_target_id", "p_hours", "p_reason",
                                        "p_training_id", "p_request_id"],
            "credit_erase_reason": ["p_admin_id", "p_reason_id"],
        }
        for name, args in expected.items():
            ((header, _b, _f),) = self.defs[name]
            self.assertEqual(_arg_names(header), args, name)

    def test_the_return_columns_b_reads(self):
        self.assertEqual(self._sig("credit_quota")[1],
                         "o_role TEXT, o_account_kind TEXT, o_account_id UUID, o_free INT, o_reserved INT, o_spent INT, "
                         "o_member_limit INT, o_member_used INT, o_next_hours INT")
        self.assertEqual(self._sig("stop_training")[1],
                         "o_outcome TEXT, o_step INT, o_attempt INT, o_backend TEXT, o_cloud_ref TEXT, o_attempt_state TEXT, "
                         "o_cancel_attempts INT, o_managed BOOLEAN, o_cloud_job_id TEXT")
        self.assertEqual(self._sig("credit_assign")[1], "o_target_free INT, o_teacher_free INT")
        self.assertEqual(self._sig("credit_reclaim")[1], "o_target_free INT, o_teacher_free INT")
        self.assertEqual(self._sig("credit_teacher_summary")[1],
                         "o_free INT, o_assigned_free INT, o_assigned_reserved INT, o_spent INT, o_compensated INT, "
                         "o_student_count INT, o_group_count INT, o_group_hours INT")
        self.assertEqual(self._sig("credit_reconcile")[1], "o_unsettled INT, o_orphans INT, o_drift INT, o_stale_open INT")
        self.assertEqual(self._sig("credit_history")[1],
                         "id BIGINT, at TIMESTAMPTZ, kind TEXT, hours INT, from_kind TEXT, from_id UUID, to_kind TEXT, "
                         "to_id UUID, training_id INT, member_id UUID, actor_id UUID, actor_role TEXT, reason_id BIGINT, "
                         "reason TEXT, meta JSONB")

    def test_the_new_trainings_columns_and_the_end_reasons(self):
        code = _code(_read(M052))
        alter = code[code.index("ALTER TABLE public.trainings\n  ADD COLUMN credit_scheme"):]
        alter = alter[:alter.index(";")]
        self.assertEqual(re.findall(r"ADD COLUMN (\w+)", alter), [
            "credit_scheme", "reserved_hours", "charged_hours", "settled_at", "idempotency_key", "stop_requested_at",
            "stop_requested_by_role", "lost_seconds", "lost_reasons", "charge_waiver"])
        self.assertIn("ALTER TABLE public.training_checkpoints ADD COLUMN config_complaints INT[] NOT NULL DEFAULT '{}';", code)
        ((_h, cap, _f),) = self.defs["cap_training_attempt"]
        self.assertIn("('capped','wall_capped','attempts_out','no_gpu','content_refused')", cap)
        ((_h, rq, _f),) = self.defs["requeue_training"]
        self.assertIn("('stalled','checkpoint_overdue')", rq)
        self.assertIn("('heartbeat_lost','pod_gone','modal_ended')", rq)
        publish = "('succeeded','capped','wall_capped','stopped','attempts_out','no_gpu','content_refused')"
        for name in ("claim_training_publish",):
            self.assertIn(publish, self.defs[name][0][1], name)
        for name in ("stop_training", "cancel_request_training"):
            self.assertIn(_norm("('succeeded','capped','wall_capped','stopped','attempts_out','no_gpu',\n"
                                "'content_refused')"), _norm(self.defs[name][0][1]), name)


def _full_049(name: str) -> str:
    """049's raw text of `name`: its CREATE through its GRANT line (comments kept)."""
    raw = _read(_MIG / "20261020130000_049_managed_training.sql")
    start = raw.index(f"CREATE OR REPLACE FUNCTION public.{name}(")
    grant = f"GRANT EXECUTE ON FUNCTION public.{name}({UTP_SIG}) TO anon, authenticated;\n"
    return raw[start:raw.index(grant, start) + len(grant)]


class FixRound20261101(unittest.TestCase):
    """The two owner-decided fixes of 2026-11-01 (a fresh zero-trust verifier's findings)."""

    def setUp(self):
        self.raw = _read(M052)
        self.defs = _defs_by_name(_code(self.raw))

    def test_update_training_progress_is_049_with_exactly_the_stall_clock_line_changed(self):
        old = _full_049("update_training_progress")
        self.assertEqual(old.count(UTP_049_LINE), 1)
        new = old.replace(UTP_049_LINE, UTP_052_LINE)
        self.assertEqual(self.raw.count(new), 1, "052's update_training_progress is not 049's with one line changed")
        self.assertNotIn(UTP_049_LINE, self.raw)
        self.assertEqual(self.raw.count("CREATE OR REPLACE FUNCTION public.update_training_progress("), 1)
        ((header, _b, _f),) = self.defs["update_training_progress"]
        self.assertIn("SECURITY DEFINER", header)
        # the step rise and the requeue's NULL keep restarting it
        self.assertIn("      WHEN v_step IS NOT NULL AND v_step > COALESCE(current_step, 0) THEN NOW()\n", new)

    def test_update_training_progress_keeps_its_grants_and_stays_out_of_the_service_role_loop(self):
        code = _code(self.raw)
        self.assertIn(f"REVOKE ALL ON FUNCTION public.update_training_progress({UTP_SIG}) FROM PUBLIC;", code)
        self.assertIn(f"GRANT EXECUTE ON FUNCTION public.update_training_progress({UTP_SIG}) TO anon, authenticated;", code)
        self.assertNotIn("'update_training_progress'", code[:code.rindex("DO $$")])  # not in the EXECUTE loop
        tail = code[code.rindex("DO $$"):]
        self.assertIn("pr.proname = 'update_training_progress'", tail)
        self.assertIn("lost EXECUTE on update_training_progress", tail)

    def test_finish_training_refuses_an_unproven_waiver_before_its_first_write(self):
        ((_h, body, _f),) = self.defs["finish_training"]
        refuse = body.index("ERRCODE = 'P0073'")
        self.assertLess(refuse, body.index("UPDATE public.trainings"))
        self.assertLess(body.index("FOR UPDATE"), refuse)
        proof = _norm(body[body.index("IF NOT ("):refuse])
        for needle in ("v_a.resume_from_step IS NOT DISTINCT FROM 0", "v_a.state IS NOT DISTINCT FROM 'running'",
                       "v_t.continued_from IS NULL", "v_t.train_seconds_used IS NOT DISTINCT FROM 0",
                       "NOT EXISTS (SELECT 1 FROM public.training_checkpoints c WHERE c.training_id = p_training_id "
                       "AND c.state = 'ready')"):
            self.assertIn(needle, proof, needle)
        self.assertIn("a.training_id = p_training_id AND a.attempt_no = p_attempt", _norm(body))
        self.assertIn("'charge waiver refused'", body)


MIRROR_ADDED = ("     AND EXISTS (SELECT 1 FROM public.trainings t WHERE t.id = c.training_id\n"
                "                  AND t.status IN ('queued','running','succeeded'))\n")


def _fn_049(name: str) -> str:
    raw = _read(_MIG / "20261020130000_049_managed_training.sql")
    start = raw.index(f"CREATE OR REPLACE FUNCTION public.{name}(")
    return raw[start:raw.index("$$;", start) + 3]


class FixRound2(unittest.TestCase):
    """Fix round 2 (2026-11-01): the checkpoint writers and the waiver's step condition."""

    def setUp(self):
        self.raw = _read(M052)
        self.defs = _defs_by_name(_code(self.raw))

    def test_mark_checkpoint_ready_signature_and_security(self):
        ((header, body, _f),) = self.defs["mark_checkpoint_ready"]
        self.assertEqual(_norm(_args(header)), "p_training_id INT, p_attempt INT, p_step INT")
        self.assertIn("RETURNS BOOLEAN", header)
        self.assertIn("SECURITY INVOKER", header)
        self.assertIn("SET search_path = public", header)

    def test_mark_checkpoint_ready_locks_the_row_first_and_writes_only_a_live_attempts_uploading_checkpoint(self):
        ((_h, body, _f),) = self.defs["mark_checkpoint_ready"]
        lock = body.index("FROM public.trainings t WHERE t.id = p_training_id FOR UPDATE;")
        self.assertLess(lock, body.index("training_attempts"))
        self.assertLess(lock, body.index("UPDATE public.training_checkpoints"))
        n = _norm(body)
        for needle in ("NOT v_t.managed", "v_t.status <> 'running'", "v_t.current_attempt IS DISTINCT FROM p_attempt",
                       "IF v_state IS DISTINCT FROM 'running' THEN RETURN false;",
                       "SET state = 'ready', ready_at = now()", "AND c.state = 'uploading';",
                       "AND c.state = 'ready');"):
            self.assertIn(needle, n, needle)
        self.assertEqual(n.count("UPDATE "), 1, "one write only")

    def test_no_other_052_function_writes_a_ready_checkpoint(self):
        for name, _h, body, _f in _definitions(_code(self.raw)):
            if name != "mark_checkpoint_ready":
                self.assertNotRegex(_norm(body),
                                    r"UPDATE (public\.)?training_checkpoints( \w+)? SET ((?!WHERE)[^;])*\bstate = 'ready'",
                                    name)
        self.assertRegex(_norm(self.defs["mark_checkpoint_ready"][0][1]),
                         r"UPDATE (public\.)?training_checkpoints( \w+)? SET ((?!WHERE)[^;])*\bstate = 'ready'")

    def test_claim_checkpoint_mirror_is_049_with_one_condition(self):
        old = _fn_049("claim_checkpoint_mirror")
        anchor = "     AND c.state = 'ready' AND c.mirrored_at IS NULL\n"
        self.assertEqual(old.count(anchor), 1)
        self.assertEqual(self.raw.count(old.replace(anchor, anchor + MIRROR_ADDED)), 1)

    def test_the_two_are_service_role_only_through_the_loop_and_checked(self):
        code = _code(self.raw)
        tail = code[code.rindex("DO $$"):]
        for name in ("mark_checkpoint_ready", "claim_checkpoint_mirror"):
            self.assertIn(name, _array(tail, "callables"))
            self.assertIn(name, _array(tail, "invokers"))
        self.assertIn("claim_checkpoint_mirror does not refuse a failed or canceled training", tail)
        self.assertIn("lacks the waiver''s step condition", tail)

    def test_the_waiver_needs_step_zero(self):
        ((_h, body, _f),) = self.defs["finish_training"]
        proof = _norm(body[body.index("IF NOT ("):body.index("ERRCODE = 'P0073'")])
        self.assertIn("v_t.current_step IS NOT DISTINCT FROM 0", proof)


class Rollback052(unittest.TestCase):
    def setUp(self):
        self.raw = _read(R052)
        self.code = _code(self.raw)

    def test_it_refuses_first_while_a_ledger_row_is_active(self):
        first = self.code[:self.code.index("DROP ")]
        self.assertIn("credit_scheme = 'ledger' AND status IN ('queued','running','cancel_requested')", first)
        self.assertIn("RAISE EXCEPTION", first)

    def test_it_drops_the_seven_triggers_and_every_052_function(self):
        for name, table in SEVEN_TRIGGERS:
            self.assertIn(f"DROP TRIGGER {name} ON public.{table};", self.code)
        self.assertIn("p.proname LIKE '\\_credit\\_%'", self.code)
        self.assertIn("(p.proname LIKE 'credit\\_%' AND p.proname <> 'credit_ledger_immutable')", self.code)
        for name in ("stop_training", "record_training_refusal", "trainings_guard_credit_columns", "trainings_credit_settle",
                     "trainings_credit_settle_delete", "users_credit_close", "workgroups_credit_close"):
            self.assertIn(f"'{name}'", self.code[:self.code.index("DROP FUNCTION public.start_training_safe")], name)

    def test_it_drops_the_four_re_created_signatures(self):
        for drop in ("DROP FUNCTION public.start_training_safe(UUID, TEXT, TEXT, TEXT, JSONB, INT, UUID, BOOLEAN, UUID, INT);",
                     "DROP FUNCTION public.start_continuation_safe(UUID, INT, TEXT, INT);",
                     "DROP FUNCTION public.cancel_request_training(INT, UUID, TEXT);",
                     "DROP FUNCTION public.finish_training(INT, INT, TEXT, TEXT, TEXT, TEXT, INT, TIMESTAMPTZ, TEXT);"):
            self.assertIn(drop, self.code)

    def test_it_restores_each_replaced_body_from_the_last_pre052_definition(self):
        restored = _defs_by_name(self.raw)
        self.assertEqual(set(restored), set(RESTORED) | WORKER_RPC)
        for name, sig in RESTORED.items():
            self.assertEqual(len(restored[name]), 1, name)
            old = _last_pre052(name)
            self.assertIsNotNone(old, name)
            self.assertEqual(_norm(restored[name][0][2]), _norm(old), name)
            self.assertIn(f"REVOKE ALL ON FUNCTION public.{name}({sig}) FROM PUBLIC, anon, authenticated;", self.code, name)
            self.assertIn(f"GRANT EXECUTE ON FUNCTION public.{name}({sig}) TO service_role;", self.code, name)

    def test_it_restores_049s_update_training_progress_verbatim_with_its_grants(self):
        old = _full_049("update_training_progress")
        self.assertEqual(self.raw.count(old), 1, "the rollback's update_training_progress is not 049's text")
        self.assertNotIn(UTP_052_LINE, self.raw)
        tail = self.code[self.code.rindex("DO $$"):]
        self.assertIn("md5(prosrc)", tail)
        b0 = old.index("AS $$") + len("AS $$")
        import hashlib
        self.assertIn(hashlib.md5(old[b0:old.index("$$;", b0)].encode()).hexdigest(), tail)

    def test_it_drops_mark_checkpoint_ready_and_restores_049s_mirror_claim(self):
        drop_loop = self.code[:self.code.index("DROP FUNCTION public.start_training_safe")]
        self.assertIn("'mark_checkpoint_ready'", drop_loop)
        old = _fn_049("claim_checkpoint_mirror")
        self.assertEqual(self.raw.count(old), 1)
        self.assertNotIn("AND t.status IN ('queued','running','succeeded')", self.code)
        tail = self.code[self.code.rindex("DO $$"):]
        import hashlib
        b0 = old.index("AS $$") + len("AS $$")
        self.assertIn(hashlib.md5(old[b0:old.index("$$;", b0)].encode()).hexdigest(), tail)
        self.assertIn("mark_checkpoint_ready survived", tail)

    def test_the_restored_bodies_come_from_049_050_and_the_baseline(self):
        sources = {}
        for name in RESTORED:
            for f in PRE052:
                if any(n == name for n, *_ in _definitions(_read(f))):
                    sources[name] = f.name
        self.assertEqual(sources["start_training_safe"], "20261020130000_049_managed_training.sql")
        self.assertEqual(sources["complete_cancel_training"], "20261020130000_049_managed_training.sql")
        self.assertEqual(sources["claim_checkpoint_mirror"], "20261020130000_049_managed_training.sql")
        for name in ("start_continuation_safe", "cancel_request_training", "claim_training_publish", "cap_training_attempt",
                     "requeue_training", "finish_training"):
            self.assertEqual(sources[name], "20261021120000_050_training_continuation.sql", name)
        for name in COMPAT:
            self.assertEqual(sources[name], "00000000000000_baseline.sql", name)

    def test_it_keeps_the_ledger_and_restores_the_051_privileges(self):
        self.assertNotRegex(self.code, r"DROP TABLE")
        self.assertNotRegex(self.code, r"DROP COLUMN")
        self.assertNotRegex(self.code, r"DROP TRIGGER credit_ledger_no_")
        self.assertIn("ALTER TABLE public.trainings ALTER COLUMN credit_scheme SET DEFAULT 'derived';", self.code)
        self.assertIn("GRANT INSERT, TRUNCATE, TRIGGER, REFERENCES ON public.trainings TO service_role;", self.code)

    def test_its_self_check(self):
        tail = self.code[self.code.rindex("DO $$"):]
        self.assertIn("RAISE NOTICE '052 rollback self-check passed';", tail)
        for needle in ("'start_training_safe','start_continuation_safe','finish_training'",
                       "has_table_privilege('service_role', 'public.trainings', 'INSERT')",
                       "'credit_ledger_no_change','credit_ledger_no_truncate'", "a 052 function survived",
                       "worker_token"):
            self.assertIn(needle, tail, needle)


class AssertionFile(unittest.TestCase):
    def test_every_label_of_spec_19a_is_asserted(self):
        text = _read(A052)
        labels = set(re.findall(r"PASS (T\d+)[a-z]?\b", text))
        # T1..T41 of spec §19a, T42..T46 of the two 2026-11-01 fix rounds, T47..T53 of fix round 3
        self.assertEqual(labels, {f"T{n}" for n in range(1, 54)})
        self.assertIn("PASS T21w (W8)", text)

    def test_it_never_calls_a_function_under_set_role(self):
        text = _read(A052)
        for block in re.findall(r"DO \$\$ BEGIN SET LOCAL ROLE \w+;(.*?)END \$\$;", text, re.S):
            body = re.sub(r"INSERT INTO\s+[\w.]+\s*\(", "INSERT INTO x VALUES", block.split("EXCEPTION")[0])
            self.assertNotRegex(body, r"(public\.\w+|pg_temp\.\w+)\s*\(", block[:120])
            self.assertNotRegex(body, r"\bPERFORM\b(?! 1 FROM)", block[:120])


if __name__ == "__main__":
    unittest.main()
