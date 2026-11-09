-- 053 — retire the classroom Jetson: drop every database object it used.
--
-- WHY. The classroom Jetson Orin Nano (migrations 019, 020, 022) is retired
-- (owner decision 2026-11-09: no Jetson is in use at any school). Its agent,
-- its cloud routes and its React flow are deleted in the same change; the
-- objects below have no reader and no writer left. Inference runs „In der
-- Cloud" (048).
--
-- WHAT IS DROPPED (exactly the Jetson objects; nothing else is touched):
--   * public.jetsons — the table, with what lives on it: its 5 indexes (019 +
--     025's idx_jetsons_intent_teacher_id), its 4 RLS policies (024), the
--     trg_jetsons_touch trigger, its FKs to classrooms/users, its grants and
--     its supabase_realtime membership (removed explicitly first);
--   * public.inference_runs.jetson_id (029) — Jetson-only: it named the
--     device a run used; its FK to jetsons goes with it. The table, its rows
--     (old backend='jetson' rows included) and 048's backend CHECK STAY: they
--     are Cloud-Inferenz's run log and the teacher's history (the table's
--     comment, which described the Jetson self-write, is rewritten);
--   * the 11 RPCs: claim_jetson, release_jetson, heartbeat_jetson,
--     agent_heartbeat_jetson, force_release_jetson, sweep_jetson_locks (019);
--     agent_release_jetson, unpair_jetson (020); regenerate_pairing_code (020,
--     rewritten by 022); claim_pair_intent and the 5-argument pair_jetson
--     (022). The 4-argument pair_jetson that 022 already dropped is dropped
--     again IF EXISTS.
-- KEPT: public.touch_updated_at() (shared by other tables' triggers) and every
-- rls_* helper (none of them reads jetsons).
--
-- DATA. Every jetsons row is lost (pairings, agent tokens) and so is every
-- inference_runs.jetson_id value. The rollback restores the schema, not the data.
--
-- DEPLOY ORDER (golden order W1 → W2). The cloud API of the same release probes
-- no Jetson object, so it boots on a database with or without 053. The OLDER
-- API still running between W1 and W2 does not: a restart in that window fails
-- its boot schema probe (jetsons missing) and SIGTERMs until W2 deploys; its
-- running process logs a failed sweep_jetson_locks every minute; and its
-- Cloud-Inferenz session sweep still sends jetson_id with each teacher run row,
-- which PostgREST refuses, so a cloud run that ends in that window gets no
-- teacher row (mark-then-insert: lost, never duplicated). Rollback, in THIS
-- order: rollback/20261109120000_053_retire_jetson_rollback.sql FIRST (the API
-- of this release boots on either schema), THEN the cloud API back to the older
-- release (which needs the restored objects to pass its boot probe).
--
-- Idempotent: every DROP is IF EXISTS. The closing DO block aborts the migration
-- if any dropped object is still there or inference_runs lost its backend CHECK.

BEGIN;

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_publication_tables
     WHERE pubname = 'supabase_realtime' AND schemaname = 'public' AND tablename = 'jetsons'
  ) THEN
    ALTER PUBLICATION supabase_realtime DROP TABLE public.jetsons;
  END IF;
END
$$;

ALTER TABLE IF EXISTS public.inference_runs DROP COLUMN IF EXISTS jetson_id;

-- 029's comment described the Jetson self-write path, which is gone.
COMMENT ON TABLE public.inference_runs IS
  'Compact per-run inference records, read by the owning teacher. Cloud-Inferenz runs (backend = ''cloud'') are written by the cloud API''s session sweep (service role); rows with backend = ''jetson'' are older runs on the retired classroom Jetson (written before migration 053) and stay readable. RLS deliberately has no permissive policies — the service-role API with its ownership asserts is the only path.';

-- Functions before the table: claim_jetson RETURNS public.jetsons, so the
-- table's row type has a dependent until it is gone.
DROP FUNCTION IF EXISTS public.claim_jetson(UUID, UUID);
DROP FUNCTION IF EXISTS public.release_jetson(UUID, UUID);
DROP FUNCTION IF EXISTS public.heartbeat_jetson(UUID, UUID);
DROP FUNCTION IF EXISTS public.agent_heartbeat_jetson(UUID, UUID, TEXT, TEXT);
DROP FUNCTION IF EXISTS public.pair_jetson(UUID, TEXT, UUID, TEXT);
DROP FUNCTION IF EXISTS public.pair_jetson(UUID, TEXT, UUID, TEXT, UUID);
DROP FUNCTION IF EXISTS public.claim_pair_intent(TEXT, UUID);
DROP FUNCTION IF EXISTS public.force_release_jetson(UUID, UUID);
DROP FUNCTION IF EXISTS public.sweep_jetson_locks();
DROP FUNCTION IF EXISTS public.agent_release_jetson(UUID, UUID);
DROP FUNCTION IF EXISTS public.regenerate_pairing_code(UUID, UUID, TEXT, TIMESTAMPTZ);
DROP FUNCTION IF EXISTS public.unpair_jetson(UUID, UUID);

DROP TABLE IF EXISTS public.jetsons;

DO $$
DECLARE
  v_fn TEXT;
BEGIN
  IF to_regclass('public.jetsons') IS NOT NULL THEN
    RAISE EXCEPTION '053: public.jetsons still exists';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_type WHERE typname = 'jetsons' AND typnamespace = 'public'::regnamespace) THEN
    RAISE EXCEPTION '053: the public.jetsons row type still exists';
  END IF;
  FOREACH v_fn IN ARRAY ARRAY[
    'claim_jetson', 'release_jetson', 'heartbeat_jetson', 'agent_heartbeat_jetson',
    'pair_jetson', 'claim_pair_intent', 'force_release_jetson', 'sweep_jetson_locks',
    'agent_release_jetson', 'regenerate_pairing_code', 'unpair_jetson'
  ] LOOP
    IF EXISTS (
      SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
       WHERE n.nspname = 'public' AND p.proname = v_fn
    ) THEN
      RAISE EXCEPTION '053: public.% still exists', v_fn;
    END IF;
  END LOOP;
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
     WHERE table_schema = 'public' AND table_name = 'inference_runs' AND column_name = 'jetson_id'
  ) THEN
    RAISE EXCEPTION '053: public.inference_runs.jetson_id still exists';
  END IF;
  IF to_regclass('public.inference_runs') IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid = 'public.inference_runs'::regclass AND conname = 'inference_runs_backend_check'
  ) THEN
    RAISE EXCEPTION '053: public.inference_runs lost its backend CHECK';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_publication_tables WHERE tablename = 'jetsons' AND schemaname = 'public') THEN
    RAISE EXCEPTION '053: jetsons is still in a publication';
  END IF;
END
$$;

COMMIT;
