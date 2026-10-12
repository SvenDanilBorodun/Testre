-- Rollback of 048 (Cloud-Inferenz). Roll the cloud API back FIRST (W2: an API that still serves
-- /inference/* or selects inference_runs.backend 500s without this schema). Drops every session row
-- (the history of cloud runs in inference_runs stays; an older API shows those rows untagged). Live
-- Modal sessions are not terminated by this: stop the inference app (`modal app stop
-- edubotics-inference`) before, or they end by their idle timeout.
BEGIN;
DROP FUNCTION IF EXISTS public.inference_session_end(TEXT, TEXT, TEXT, TEXT, JSONB);
DROP FUNCTION IF EXISTS public.inference_session_heartbeat(TEXT, TEXT, TEXT, JSONB);
DROP FUNCTION IF EXISTS public.inference_session_connect(TEXT, TEXT, TEXT);
DROP FUNCTION IF EXISTS public.inference_session_peek(TEXT, TEXT);
DROP FUNCTION IF EXISTS public.inference_usage(UUID, INT);
DROP FUNCTION IF EXISTS public.heartbeat_inference_session_client(UUID, UUID);
DROP FUNCTION IF EXISTS public.mark_inference_edge_terminated(UUID);
DROP FUNCTION IF EXISTS public.inference_edges_due(INT, INT);
DROP FUNCTION IF EXISTS public.sweep_inference_sessions();
DROP FUNCTION IF EXISTS public.end_inference_session(UUID, UUID, TEXT);
DROP FUNCTION IF EXISTS public.arm_inference_session(UUID, UUID, TEXT);
DROP FUNCTION IF EXISTS public.attach_inference_edge(UUID, TEXT, TEXT);
DROP FUNCTION IF EXISTS public.create_inference_session_safe(UUID, INT, TEXT, TEXT, TEXT, TEXT, INT, INT, INT, INT);
DROP FUNCTION IF EXISTS public._inference_used_today_s(UUID);
DROP FUNCTION IF EXISTS public._inference_session_expire_if_due(UUID);
DROP FUNCTION IF EXISTS public._inference_session_finish(UUID, TEXT);
DROP TABLE IF EXISTS public.inference_sessions;
ALTER TABLE public.inference_runs DROP CONSTRAINT IF EXISTS inference_runs_backend_check;
ALTER TABLE public.inference_runs DROP COLUMN IF EXISTS backend;
COMMIT;
