-- Rollback for 002_accounts.sql
-- *** WARNING: drops every admin/teacher/student account row plus every
-- *** classroom. Run only if you genuinely want to reset the account
-- *** system. Take a Supabase PITR snapshot first.

BEGIN;

-- Drop RPCs
DROP FUNCTION IF EXISTS public.adjust_student_credits(UUID, UUID, INTEGER);
DROP FUNCTION IF EXISTS public.get_teacher_credit_summary(UUID);

-- Drop trigger + its function. Names must match 002_accounts.sql exactly:
-- trigger is `trg_classroom_capacity` on `public.users`, function is
-- `enforce_classroom_capacity()`. IF EXISTS silently succeeds on a typo,
-- so the previous wrong names would have left a stale trigger referencing
-- `NEW.role` *after* we drop the role column below -> every subsequent
-- write to public.users would error.
DROP TRIGGER IF EXISTS trg_classroom_capacity ON public.users;
DROP FUNCTION IF EXISTS public.enforce_classroom_capacity() CASCADE;

-- Drop classrooms (cascade would remove FKs on users)
DROP TABLE IF EXISTS public.classrooms CASCADE;

-- Drop account columns added to users
ALTER TABLE public.users DROP COLUMN IF EXISTS role;
ALTER TABLE public.users DROP COLUMN IF EXISTS username;
ALTER TABLE public.users DROP COLUMN IF EXISTS full_name;
ALTER TABLE public.users DROP COLUMN IF EXISTS classroom_id;
ALTER TABLE public.users DROP COLUMN IF EXISTS created_by;

-- Drop role enum (must be unreferenced at this point)
DROP TYPE IF EXISTS public.user_role;

COMMIT;
