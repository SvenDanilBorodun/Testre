-- Rollback for 029_inference_runs: drop the table (records are
-- diagnostics only — no FK points AT inference_runs, so the drop is
-- self-contained). Take a PITR snapshot first.

DROP TABLE IF EXISTS public.inference_runs;
