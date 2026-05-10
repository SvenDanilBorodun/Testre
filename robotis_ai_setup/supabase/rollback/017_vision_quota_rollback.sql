-- 017_vision_quota_rollback.sql

BEGIN;

DROP FUNCTION IF EXISTS public.reset_vision_quota_used();
DROP FUNCTION IF EXISTS public.consume_vision_quota(UUID);
ALTER TABLE public.users
  DROP COLUMN IF EXISTS vision_used_per_term,
  DROP COLUMN IF EXISTS vision_quota_per_term;

COMMIT;
