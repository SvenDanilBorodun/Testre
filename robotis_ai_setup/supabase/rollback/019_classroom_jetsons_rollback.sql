-- 019_classroom_jetsons_rollback.sql

BEGIN;

-- Realtime publication first (idempotent — only drop if present).
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_publication_tables
     WHERE pubname = 'supabase_realtime'
       AND schemaname = 'public'
       AND tablename = 'jetsons'
  ) THEN
    ALTER PUBLICATION supabase_realtime DROP TABLE public.jetsons;
  END IF;
END
$$;

DROP TRIGGER IF EXISTS trg_jetsons_touch ON public.jetsons;

DROP POLICY IF EXISTS "Admins manage jetsons" ON public.jetsons;
DROP POLICY IF EXISTS "Teachers manage own classroom jetson" ON public.jetsons;
DROP POLICY IF EXISTS "Classroom members read classroom jetson" ON public.jetsons;

DROP FUNCTION IF EXISTS public.sweep_jetson_locks();
DROP FUNCTION IF EXISTS public.force_release_jetson(UUID, UUID);
DROP FUNCTION IF EXISTS public.pair_jetson(UUID, TEXT, UUID, TEXT);
DROP FUNCTION IF EXISTS public.agent_heartbeat_jetson(UUID, UUID, TEXT, TEXT);
DROP FUNCTION IF EXISTS public.heartbeat_jetson(UUID, UUID);
DROP FUNCTION IF EXISTS public.release_jetson(UUID, UUID);
DROP FUNCTION IF EXISTS public.claim_jetson(UUID, UUID);

DROP TABLE IF EXISTS public.jetsons;

-- touch_updated_at is shared across migrations — do NOT drop it here.

COMMIT;
