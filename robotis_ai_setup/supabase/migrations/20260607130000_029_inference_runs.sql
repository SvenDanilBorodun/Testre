-- 029_inference_runs.sql
--
-- leLab-comparison PR-5b (2026-06-07). Jetson inference is ephemeral by
-- design: the per-session volume is wiped on disconnect and container
-- logs die with it — when a student's run failed, neither the student
-- nor the teacher could later see WHY. The React app (which watches the
-- /task/status phases and holds the student JWT) now POSTs ONE compact
-- record per finished/failed Jetson inference run; teachers read them
-- per student in the dashboard drawer.
--
-- Authorization model (Rule §4 — service-role bypasses RLS, the Python
-- asserts are the real layer):
--   * WRITE: student-self only. routes/jetson.py keys the row to the
--     verified JWT's user id and the profile's classroom — NEVER to any
--     body field (a body user_id would be a textbook IDOR under the
--     service-role client).
--   * READ: teacher via _assert_student_owned (the existing helper).
-- RLS is enabled with no permissive policies — defense-in-depth: any
-- future anon/authenticated PostgREST access hits a closed table.
--
-- Retention: bounded per student by the route (keep the newest 50 —
-- delete older on insert), so a looping classroom session can't grow
-- the table unboundedly.
--
-- Idempotent — re-runnable.

BEGIN;

CREATE TABLE IF NOT EXISTS public.inference_runs (
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  student_user_id   UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
  classroom_id      UUID REFERENCES public.classrooms(id) ON DELETE SET NULL,
  jetson_id         UUID REFERENCES public.jetsons(id) ON DELETE SET NULL,
  policy_repo       TEXT NOT NULL,
  started_at        TIMESTAMPTZ,
  duration_s        REAL,
  -- 'finished' | 'error' | 'stopped' (client-reported; informational)
  exit_reason       TEXT NOT NULL,
  -- The German TaskStatus.error the student saw (empty for clean runs).
  error_message_de  TEXT NOT NULL DEFAULT '',
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_inference_runs_student_created
  ON public.inference_runs (student_user_id, created_at DESC);

ALTER TABLE public.inference_runs ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE public.inference_runs IS
  'Compact per-run Jetson inference records (leLab-comparison PR-5b). '
  'Written student-self via the cloud API (JWT-keyed); read by the '
  'owning teacher. RLS deliberately has no permissive policies — the '
  'service-role API with its ownership asserts is the only path.';

COMMIT;
