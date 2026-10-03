-- Rollback for 042_user_hf_credentials. Drops the writer, the touch trigger and
-- the table. users.hf_username is NOT touched: a name the migration proved stays
-- as an ordinary 030 anchor, which is exactly what PATCH /me wrote before 042.
--
-- DATA: every stored student token is DELETED with the table. Nothing else is
-- lost; each student pastes their token once more after the next roll-forward.
-- Roll the cloud API back FIRST (the 042 API probes this table and calls the RPC,
-- so its /health would fail against a database without them).
BEGIN;

DROP FUNCTION IF EXISTS public.store_user_hf_credential(uuid, text, text, text, text, text);
DROP TRIGGER IF EXISTS user_hf_credentials_touch ON public.user_hf_credentials;
DROP FUNCTION IF EXISTS public.user_hf_credentials_touch();
DROP TABLE IF EXISTS public.user_hf_credentials;

COMMIT;
