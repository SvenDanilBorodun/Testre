"""„Abbrechen"'s preview says what the stop then does (fix round 3, mutants AP2 / AP3).

``routes/training.py::stop_preview_for`` duplicates two rules of migration 052: the
stop decision of ``stop_training`` (§4.9: publish the newest ready checkpoint after a
GPU trained, else cancel; a ``cancel_requested`` row and a pre-052 ``derived`` row always
cancel) and the settlement (``_credit_settle``: charge = min(reserved, ceil(seconds /
3600)), 0 when no attempt ever had a first running write; ``complete_cancel_training``
counts the open attempt only to the FIRST stop request).

``STOP_STATES`` is the one table of states both halves are held to:

* ``PreviewFollowsTheTable`` asks ``stop_preview_for`` for each state;
* ``FakeStopFollowsTheTable`` runs the same state through the fake's ``stop_training``
  and then the step that settles it (``complete_cancel_training`` for a cancel, the
  publish's ``finish_training(succeeded)`` otherwise) and reads the outcome and the
  charged hours back.

The fake follows the SQL statement by statement (``_managed_fakes.py``) and the SQL is
proven by ``supabase/tests/052_credit_ledger_assertions.sql`` (T16, T17, T34, T52), so a
drift of the preview, or of the fake away from it, fails one of the two classes. Two
rows exist for the preview's two duplicated gates: ``counted_without_heartbeat`` (the
trained gate: seconds counted while no attempt ever had a first running write — a
constructed state, since the API counts seconds only from a first heartbeat — charge 0)
and ``stop_requested_earlier`` (the stop bound: the open attempt counts to the first stop
request, not to now).
"""

from __future__ import annotations

import math
import unittest

import app.tests.test_managed_start  # noqa: F401  (stubs installed there)

from app.routes import training as route  # noqa: E402
from app.tests._managed_fakes import Clock, FakeDB  # noqa: E402

MIN = 60
STUDENT = "44444444-4444-4444-8444-444444444444"
TOTAL = 50000

# one row per state: the training row, its attempts (attempt_no, state, end_reason, seconds
# since the first running write or None), its ready checkpoints (attempt_no, step), and what
# stopping NOW does: the SQL outcome, the published step and the hours charged in the end.
STOP_STATES = [
    dict(name="never_trained",
         row=dict(status="queued", current_attempt=1),
         attempts=[(1, "starting", None, None)], ready=[],
         expect=dict(outcome="cancel", step=None, charged=0)),
    dict(name="counted_without_heartbeat",
         row=dict(status="running", current_attempt=1, train_seconds_used=90 * MIN),
         attempts=[(1, "starting", None, None)], ready=[],
         expect=dict(outcome="cancel", step=None, charged=0)),
    dict(name="trained_no_checkpoint",
         row=dict(status="running", current_attempt=1),
         attempts=[(1, "running", None, 40 * MIN)], ready=[],
         expect=dict(outcome="cancel", step=None, charged=1)),
    dict(name="trained_ready_checkpoint",
         row=dict(status="running", current_attempt=1),
         attempts=[(1, "running", None, 80 * MIN)], ready=[(1, 2500)],
         expect=dict(outcome="publish", step=2500, charged=2)),
    dict(name="trained_final_ready",
         row=dict(status="running", current_attempt=1),
         attempts=[(1, "running", None, 130 * MIN)], ready=[(1, 2500), (1, TOTAL)],
         expect=dict(outcome="publish_final", step=TOTAL, charged=3)),
    dict(name="stop_requested_earlier",
         row=dict(status="cancel_requested", current_attempt=1, cancel_attempts=1, stop_requested_ago=30 * MIN,
                  stop_requested_by_role="student"),
         attempts=[(1, "running", None, 80 * MIN)], ready=[(1, 2500)],
         expect=dict(outcome="cancel", step=None, charged=1)),
    dict(name="lost_attempt_between_attempts",
         row=dict(status="queued", current_attempt=1, train_seconds_used=60 * MIN),
         attempts=[(1, "lost", "pod_gone", 120 * MIN)], ready=[(1, 2500)],
         expect=dict(outcome="publish", step=2500, charged=1)),
    dict(name="one_second_past_the_hour",
         row=dict(status="running", current_attempt=2, train_seconds_used=60 * MIN),
         attempts=[(1, "lost", "pod_gone", 200 * MIN), (2, "running", None, 1)], ready=[],
         expect=dict(outcome="cancel", step=None, charged=2)),
    dict(name="reservation_caps_the_charge",
         row=dict(status="running", current_attempt=1, reserved_hours=1),
         attempts=[(1, "running", None, 150 * MIN)], ready=[],
         expect=dict(outcome="cancel", step=None, charged=1)),
    dict(name="derived_row_with_a_ready_checkpoint",
         row=dict(status="running", current_attempt=1, credit_scheme="derived", reserved_hours=None),
         attempts=[(1, "running", None, 40 * MIN)], ready=[(1, 2500)],
         expect=dict(outcome="cancel", step=None, charged=0)),
]


def build(db: FakeDB, clock: Clock, state: dict) -> dict:
    """The state as rows of the fake database; returns the trainings row."""
    spec = dict(state["row"])
    ago = spec.pop("stop_requested_ago", None)
    spec.setdefault("credit_scheme", "ledger")
    spec.setdefault("reserved_hours", 3)
    if ago is not None:
        spec["stop_requested_at"] = clock.iso(clock.t - ago)
    row = db.add_training(user_id=STUDENT, **spec)
    for n, st, end_reason, hb in state["attempts"]:
        db.add_attempt(row["id"], n, state=st, end_reason=end_reason, backend="runpod_secure",
                       cloud_ref=f"pod-{row['id']}-{n}",
                       first_heartbeat_at=None if hb is None else clock.iso(clock.t - hb))
    for n, step in state["ready"]:
        db.add_checkpoint(row["id"], n, step, state="ready", manifest={})
    return row


class TheTable(unittest.TestCase):
    def test_every_state_is_named_once_and_each_duplicated_gate_has_a_row(self):
        names = [s["name"] for s in STOP_STATES]
        self.assertEqual(len(names), len(set(names)))
        self.assertIn("counted_without_heartbeat", names)   # the trained gate (AP2)
        self.assertIn("stop_requested_earlier", names)      # the stop bound (AP3)

    def test_the_two_gate_rows_tell_the_rules_apart(self):
        # without the trained gate the counted 90 min would charge 2; without the stop
        # bound the open attempt's 80 min (not 50) would charge 2: each row has teeth
        by = {s["name"]: s for s in STOP_STATES}
        self.assertEqual(math.ceil(by["counted_without_heartbeat"]["row"]["train_seconds_used"] / 3600), 2)
        self.assertEqual(by["counted_without_heartbeat"]["expect"]["charged"], 0)
        self.assertEqual(math.ceil(80 * MIN / 3600), 2)
        self.assertEqual(by["stop_requested_earlier"]["expect"]["charged"], 1)


class PreviewFollowsTheTable(unittest.TestCase):
    def test_the_preview_of_every_state(self):
        for state in STOP_STATES:
            with self.subTest(state=state["name"]):
                clock = Clock()
                db = FakeDB(clock)
                row = build(db, clock, state)
                attempts = [a for a in db.tables["training_attempts"] if a["training_id"] == row["id"]]
                ready = [c for c in db.tables["training_checkpoints"]
                         if c["training_id"] == row["id"] and c["state"] == "ready"]
                out = route.stop_preview_for(row, attempts, ready, clock.t)
                want = state["expect"]
                reserved = int(row.get("reserved_hours") or 0)
                self.assertEqual(out["outcome"], "cancel" if want["outcome"] == "cancel" else "publish")
                self.assertEqual(out["checkpoint_step"], want["step"])
                self.assertEqual(out["final_ready"], any(step == TOTAL for _, step in state["ready"]))
                self.assertEqual(out["trained"], any(hb is not None for *_, hb in state["attempts"]))
                self.assertEqual(out["charged_hours_now"], want["charged"])
                self.assertEqual(out["hours_back_now"], max(reserved - want["charged"], 0))


class FakeStopFollowsTheTable(unittest.TestCase):
    def test_stop_training_then_its_settlement_for_every_state(self):
        for state in STOP_STATES:
            with self.subTest(state=state["name"]):
                clock = Clock()
                db = FakeDB(clock)
                row = build(db, clock, state)
                tid = row["id"]
                (res,) = db._rpc_stop_training(tid, STUDENT, STUDENT, "student", None)
                want = state["expect"]
                self.assertEqual((res["o_outcome"], res["o_step"]), (want["outcome"], want["step"]))
                if res["o_outcome"] == "cancel":
                    self.assertEqual(db.row(tid)["status"], "cancel_requested")
                    db._rpc_complete_cancel_training(tid, "canceled", None)
                else:                                    # the publish executor's finish
                    self.assertEqual(db.row(tid)["status"], "running")
                    db._rpc_finish_training(tid, row["current_attempt"], "succeeded", None, None,
                                            p_final_step=res["o_step"])
                done = db.row(tid)
                if done["credit_scheme"] == "ledger":
                    self.assertEqual(done["charged_hours"], want["charged"])
                else:                                    # a derived row is never settled
                    self.assertIsNone(done["charged_hours"])
                    self.assertEqual(want["charged"], 0)


if __name__ == "__main__":
    unittest.main()
