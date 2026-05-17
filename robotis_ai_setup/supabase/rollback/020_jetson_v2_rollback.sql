-- Rollback for 020_jetson_v2.sql
-- Drops the three follow-up RPCs added in v2.3.0. Safe to run multiple
-- times. Does NOT touch migration 019's tables/RPCs/policies.

BEGIN;

DROP FUNCTION IF EXISTS public.agent_release_jetson(UUID, UUID);
DROP FUNCTION IF EXISTS public.regenerate_pairing_code(UUID, UUID, TEXT, TIMESTAMPTZ);
DROP FUNCTION IF EXISTS public.unpair_jetson(UUID, UUID);

COMMIT;
