-- 057 — the request roles are default-deny: anon and authenticated may READ
-- only the columns a shipped client provably reads (or that RLS needs to
-- evaluate those reads), WRITE no table, sequence or column in schema public,
-- and EXECUTE only an explicit allowlist of functions; and nothing a later
-- migration creates grants them anything unless that migration says so
-- (security audit 2026-11-10, SA-10, the root cause behind 046, 047 and 055;
-- SA-01's database half; SA-22; the owner's scope extension of 2026-11-13: RLS
-- must not be the only fence for reads either).
--
-- WHY. Supabase's default privileges (pg_default_acl, schema public, owners
-- postgres and supabase_admin, read live) grant ALL on every new table and
-- sequence and EXECUTE on every new function to anon and authenticated, and
-- PostgreSQL itself grants EXECUTE on every new function to PUBLIC. RLS was the
-- only fence: after 055 both request roles could SELECT every column of thirteen
-- public tables (users' e-mails and names, teachers' notes, every program's
-- versions, submissions and recordings, the inference run log, the arm records)
-- and nine of them were also writable (INSERT, UPDATE, DELETE, TRUNCATE,
-- REFERENCES, TRIGGER); two sequences answered setval and eleven
-- trigger/helper functions EXECUTE. One permissive policy, one policy that
-- forgets a condition (SA-07: an admin's aal1 session), or one SECURITY DEFINER
-- function that forgets its REVOKE re-opened a PostgREST path (046, 047, 054 and
-- 055 each closed one after the fact). Separately, the live database carries a
-- PUBLIC storage bucket `releases` with an INSERT policy "Service role upload to
-- releases" for every signed-in user (SA-01); no migration created either.
--
-- THE AUDIT BEHIND THE ALLOWLISTS (2026-11-13, this repository; the proof of
-- every entry is in supabase/request_role_allowlist.json, the ONE source the CI
-- check reads and this file's lists are held equal to):
--   * READS. The React app (student, teacher-web and Orange-Pi builds) reads
--     public tables in exactly five places: the trainings SELECT of
--     hooks/useSupabaseTrainings.js (LIVE_COLUMNS, filtered on user_id /
--     workgroup_id, ordered by requested_at) and the Realtime postgres_changes
--     channels on trainings, datasets, workflows and tutorial_progress
--     (useSupabaseTrainings, useGroupDatasets, useSupabaseWorkflows,
--     useSupabaseTutorialProgress), each filtered on owner_user_id / user_id /
--     workgroup_id. Realtime delivers only the columns the subscriber may
--     SELECT, checks the row through the table's RLS as the subscriber, and
--     matches its filter on the delivered record: the kept columns are the
--     primary key, the filter columns and every field the consumers read
--     (DatasetSelector: id, hf_repo_id, name; TemplatePicker: id, name,
--     description, is_template, updated_at; SkillmapPlayer: tutorial_id,
--     current_step, completed_at, updated_at; both merges sort by updated_at).
--     Every read is an authenticated session; nothing reads as anon. A policy's
--     sub-select runs with the reader's privileges, so the columns the read
--     policies of those four tables reach in users, classrooms, workgroups and
--     workgroup_memberships (pg_depend of the policies, closed transitively)
--     stay readable too: users(id, role, classroom_id, workgroup_id),
--     classrooms(id, teacher_id), workgroups(id, classroom_id),
--     workgroup_memberships(user_id, workgroup_id) — RLS still decides the rows.
--     The teacher web and the admin dashboard read through the cloud API
--     (service role); the training worker and the inference container call
--     RPCs only; the Pi agent, the Windows GUI and the robot images make no
--     Supabase call; tools/feetech_provision.py uses the service-role key.
--   * WRITES: none is legitimate. Every writer is the service role or a
--     SECURITY DEFINER function (runs as its owner); workflow_versions is written
--     only by the SECURITY DEFINER snapshot_workflow_version() trigger, so its
--     INSERT policy (027) could only ever judge a request role and is dropped
--     here (055 dropped users' own-row policy for the same reason).
--   * SEQUENCES: nobody but the owner and the service role inserts.
--   * FUNCTIONS: the three rls_* helpers for authenticated (every function a
--     public policy calls, from pg_depend; anon reads no table, so it evaluates
--     no policy), update_training_progress (anon and authenticated, as since
--     010/032) and the four anon inference RPCs. A trigger function needs no
--     EXECUTE grant (PostgreSQL checks it when the trigger is created, never when
--     it fires, 054). The three jsonb_* helpers of the workflows /
--     workflow_submissions CHECK constraints are evaluated for the writer, the
--     service role or a definer's owner.
--
-- THE FIX:
--   1. REVOKE ALL on every table, view and foreign table in public FROM PUBLIC,
--      anon, authenticated, at table level and at column level (MAINTAIN, which
--      PostgreSQL 17 adds, included), then GRANT SELECT on the read allowlist's
--      columns to authenticated. Every RLS policy is kept as it is (056's aal2
--      admin rule stays the row fence of the kept reads); service_role and the
--      Realtime publication are untouched.
--   2. REVOKE ALL on every sequence in public from the same three.
--   3. REVOKE EXECUTE on every routine in public FROM PUBLIC, anon,
--      authenticated, then GRANT the function allowlist back by signature. A
--      function the service role reached only through PUBLIC is granted to
--      service_role by name first (none today; the self-check compares every
--      service_role privilege before and after).
--   4. DROP POLICY "Trigger inserts workflow versions" (027).
--   5. Default privileges, for every role that owns objects in public or has a
--      default ACL there and whose defaults the migrating role may change: in
--      schema public nothing for PUBLIC, anon or authenticated on TABLES,
--      SEQUENCES and FUNCTIONS; and GLOBALLY (no IN SCHEMA) no EXECUTE for
--      PUBLIC on FUNCTIONS — PostgreSQL's built-in default grants it, and a
--      per-schema REVOKE cannot remove a global grant — with service_role given
--      that EXECUTE instead, globally. A role the migrating role is not a member
--      of (supabase_admin on the hosted project, whose objects no migration
--      creates) is reported with a NOTICE and left for the owner. The global
--      default also covers a pg_temp function: a hand-run assertion helper
--      called under SET ROLE needs its own GRANT (047's and 054's files).
--   6. Storage (SA-01, database half): DROP the "Service role upload to
--      releases" policy if it is still there and make the bucket `releases`
--      private if it still exists and is public; no object is deleted. Both are
--      skipped when the owner already removed them by hand and when the
--      database has no storage schema (a stub replay). If the migrating role may
--      not change them, the migration stops and names the dashboard step.
--   7. pg_stat_statements (SA-22): REVOKE SELECT on
--      extensions.pg_stat_statements(_info) from anon and authenticated when the
--      view exists; a read left through PUBLIC (the extension's own grant) or
--      a grant this role did not make is a WARNING, never an abort.
--   8. A self-check that aborts (and so rolls back) the migration on: a table-
--      level privilege of PUBLIC or a request role on any public relation; a
--      column SELECT outside the read allowlist or an allowlisted one missing;
--      any column-level write; any sequence privilege; a request-role EXECUTE
--      outside the function allowlist or an allowlisted one missing; a policy
--      of a readable table calling a function, or sub-selecting a column, its
--      reader may not use; a write policy left for a request role; a changed
--      service_role privilege; a default privilege (catalog AND a probe table,
--      sequence and function created and dropped in this transaction) granting
--      a request role or PUBLIC anything; a storage policy for a request role; a
--      public bucket; trainings leaving the Realtime publication.
--
-- AFTER 057 a new table, column, sequence or function is invisible and
-- uncallable for anon and authenticated until its own migration grants it AND
-- request_role_allowlist.json names it (CLAUDE.md Rule 4).
--
-- PROVEN 2026-11-13 on PostgreSQL 16 with .github/scripts/supabase_replay_stub.sql
-- (the stub ci.yml::migrations-replay uses), every migration 000 → 055 applied
-- by psql (and 000 → 058 by `supabase db push --include-all` 2.122.0), then this
-- file: supabase/tests/057_request_role_default_deny_assertions.sql (its header
-- has the PASS count, the cycle and the older labels 057 breaks by design).
--
-- API: no table, column or RPC the boot schema probe names changes shape, and
-- the service role keeps every privilege. PostgREST answers a request-role read
-- of a column outside the allowlist, any request-role write and any call of a
-- function outside the allowlist with 42501.
--
-- ROLLBACK: rollback/20261113120000_057_request_role_default_deny_rollback.sql
-- re-grants the pre-057 table, column, sequence, function and default privileges
-- and re-creates 027's policy (it RE-OPENS the request-role read and write
-- hole); the storage policy and bucket are not restored.
--
-- Idempotent: running it again revokes the same, re-grants the same lists, and
-- every step that finds nothing to do is a no-op.

BEGIN;

-- The allowlists (supabase/request_role_allowlist.json holds the same lists and
-- the proof of every entry; tests/test_supabase_057.py holds them equal).
CREATE TEMP TABLE _057_read_allow (rel TEXT, roles TEXT[], cols TEXT[]) ON COMMIT DROP;
INSERT INTO _057_read_allow VALUES
    ('public.trainings', ARRAY['authenticated'],
     ARRAY['id', 'user_id', 'status', 'model_name', 'model_type', 'dataset_name', 'current_step', 'total_steps',
           'current_loss', 'loss_history', 'error_message', 'requested_at', 'terminated_at', 'last_progress_at',
           'workgroup_id', 'status_note', 'capped_step', 'continuable_until', 'continued_from', 'reserved_hours',
           'charged_hours', 'stop_requested_by_role', 'lost_seconds']),
    ('public.datasets', ARRAY['authenticated'],
     ARRAY['id', 'owner_user_id', 'workgroup_id', 'hf_repo_id', 'name', 'updated_at']),
    ('public.workflows', ARRAY['authenticated'],
     ARRAY['id', 'owner_user_id', 'workgroup_id', 'name', 'description', 'is_template', 'updated_at']),
    ('public.tutorial_progress', ARRAY['authenticated'],
     ARRAY['user_id', 'tutorial_id', 'current_step', 'completed_at', 'updated_at']),
    ('public.users', ARRAY['authenticated'],
     ARRAY['id', 'role', 'classroom_id', 'workgroup_id']),
    ('public.classrooms', ARRAY['authenticated'],
     ARRAY['id', 'teacher_id']),
    ('public.workgroups', ARRAY['authenticated'],
     ARRAY['id', 'classroom_id']),
    ('public.workgroup_memberships', ARRAY['authenticated'],
     ARRAY['user_id', 'workgroup_id']);

CREATE TEMP TABLE _057_exec_allow (fn TEXT, roles TEXT[]) ON COMMIT DROP;
INSERT INTO _057_exec_allow VALUES
    ('public.rls_is_admin()', ARRAY['authenticated']),
    ('public.rls_teacher_classroom_ids()', ARRAY['authenticated']),
    ('public.rls_user_classroom_id()', ARRAY['authenticated']),
    ('public.update_training_progress(integer,uuid,text,integer,integer,real,text,text)', ARRAY['anon', 'authenticated']),
    ('public.inference_session_peek(text,text)', ARRAY['anon']),
    ('public.inference_session_connect(text,text,text)', ARRAY['anon']),
    ('public.inference_session_heartbeat(text,text,text,jsonb)', ARRAY['anon']),
    ('public.inference_session_end(text,text,text,text,jsonb)', ARRAY['anon']);

-- Everything service_role holds before any change: the self-check compares it after.
CREATE TEMP TABLE _057_service_before ON COMMIT DROP AS
SELECT 'table' AS kind, c.oid AS obj, p.priv, has_table_privilege('service_role', c.oid, p.priv) AS held
  FROM pg_class c
  CROSS JOIN (VALUES ('SELECT'), ('INSERT'), ('UPDATE'), ('DELETE'), ('TRUNCATE'), ('REFERENCES'), ('TRIGGER')) AS p (priv)
 WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
UNION ALL
SELECT 'sequence', c.oid, p.priv, has_sequence_privilege('service_role', c.oid, p.priv)
  FROM pg_class c
  CROSS JOIN (VALUES ('USAGE'), ('SELECT'), ('UPDATE')) AS p (priv)
 WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'S'
UNION ALL
SELECT 'function', p.oid, 'EXECUTE', has_function_privilege('service_role', p.oid, 'EXECUTE')
  FROM pg_proc p
 WHERE p.pronamespace = 'public'::regnamespace;

-- 3a. The service role keeps every function it reaches only through PUBLIC.
DO $$
DECLARE
    v_fn REGPROCEDURE;
BEGIN
    FOR v_fn IN
        SELECT p.oid::regprocedure FROM pg_proc p
         WHERE p.pronamespace = 'public'::regnamespace
           AND has_function_privilege('service_role', p.oid, 'EXECUTE')
           AND NOT EXISTS (SELECT 1 FROM aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a
                            WHERE a.grantee = to_regrole('service_role') AND a.privilege_type = 'EXECUTE')
    LOOP
        EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO service_role', v_fn);
    END LOOP;
END
$$;

-- 1. No request-role privilege on any table, view or foreign table (a table-
--    level REVOKE also clears the column-level grants of the same privilege;
--    the loop below is the belt) …
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC, anon, authenticated;

DO $$
DECLARE
    v_rel REGCLASS;
    v_col TEXT;
BEGIN
    FOR v_rel, v_col IN
        SELECT c.oid::regclass, a.attname
          FROM pg_class c
          JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
         WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
           AND a.attacl IS NOT NULL
    LOOP
        EXECUTE format('REVOKE SELECT (%2$I), INSERT (%2$I), UPDATE (%2$I), REFERENCES (%2$I) ON %1$s FROM PUBLIC, anon, authenticated',
                       v_rel, v_col);
    END LOOP;
END
$$;

-- … but the read allowlist, column by column.
DO $$
DECLARE
    v_row RECORD;
BEGIN
    FOR v_row IN SELECT rel, roles, cols FROM pg_temp._057_read_allow LOOP
        EXECUTE format('GRANT SELECT (%s) ON %s TO %s',
                       (SELECT string_agg(quote_ident(c), ', ') FROM unnest(v_row.cols) AS c),
                       v_row.rel::regclass,
                       (SELECT string_agg(quote_ident(r), ', ') FROM unnest(v_row.roles) AS r));
    END LOOP;
END
$$;

-- 2. No request-role privilege on any sequence.
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC, anon, authenticated;

-- 3b. No request-role EXECUTE on any routine, but the function allowlist.
REVOKE EXECUTE ON ALL ROUTINES IN SCHEMA public FROM PUBLIC, anon, authenticated;

DO $$
DECLARE
    v_row RECORD;
    v_fn  REGPROCEDURE;
BEGIN
    FOR v_row IN SELECT fn, roles FROM pg_temp._057_exec_allow LOOP
        v_fn := to_regprocedure(v_row.fn);
        IF v_fn IS NULL THEN
            RAISE EXCEPTION '057: allowlisted function % is missing', v_row.fn;
        END IF;
        EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO %s', v_fn,
                       (SELECT string_agg(quote_ident(r), ', ') FROM unnest(v_row.roles) AS r));
    END LOOP;
END
$$;

-- 4. The workflow_versions INSERT policy can never apply any more.
DROP POLICY IF EXISTS "Trigger inserts workflow versions" ON public.workflow_versions;

-- 5. Default privileges.
DO $$
DECLARE
    v_owner NAME;
BEGIN
    FOR v_owner IN
        SELECT r.rolname FROM pg_roles r
         WHERE r.rolname = current_user
            OR r.oid IN (SELECT d.defaclrole FROM pg_default_acl d
                          WHERE d.defaclnamespace IN (0, 'public'::regnamespace))
            OR r.oid IN (SELECT c.relowner FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace)
            OR r.oid IN (SELECT p.proowner FROM pg_proc p WHERE p.pronamespace = 'public'::regnamespace)
         ORDER BY r.rolname
    LOOP
        IF NOT pg_has_role(current_user, v_owner, 'USAGE') THEN
            RAISE NOTICE '057: % may not change the default privileges of %; objects % creates in public still follow them (an owner setting, docs/KNOWN-ISSUES.md)',
                current_user, v_owner, v_owner;
            CONTINUE;
        END IF;
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public REVOKE ALL ON TABLES FROM PUBLIC, anon, authenticated', v_owner);
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public REVOKE ALL ON SEQUENCES FROM PUBLIC, anon, authenticated', v_owner);
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC, anon, authenticated', v_owner);
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I REVOKE ALL ON TABLES FROM PUBLIC, anon, authenticated', v_owner);
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I REVOKE ALL ON SEQUENCES FROM PUBLIC, anon, authenticated', v_owner);
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC, anon, authenticated', v_owner);
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I GRANT EXECUTE ON FUNCTIONS TO service_role', v_owner);
    END LOOP;
END
$$;

-- 6. Storage: the live drift (SA-01).
DO $$
BEGIN
    IF to_regclass('storage.objects') IS NOT NULL
       AND EXISTS (SELECT 1 FROM pg_policy
                    WHERE polrelid = 'storage.objects'::regclass
                      AND polname = 'Service role upload to releases') THEN
        BEGIN
            EXECUTE 'DROP POLICY "Service role upload to releases" ON storage.objects';
        EXCEPTION WHEN insufficient_privilege THEN
            RAISE EXCEPTION '057: % may not drop the storage policy "Service role upload to releases"; delete it in the dashboard (Storage, Policies) and apply 057 again',
                current_user;
        END;
    END IF;
    IF to_regclass('storage.buckets') IS NOT NULL THEN
        BEGIN
            EXECUTE 'UPDATE storage.buckets SET public = false WHERE id = ''releases'' AND public';
        EXCEPTION WHEN insufficient_privilege THEN
            RAISE EXCEPTION '057: % may not make the storage bucket "releases" private; delete it (or make it private) in the dashboard and apply 057 again',
                current_user;
        END;
    END IF;
END
$$;

-- 7. pg_stat_statements (SA-22).
DO $$
DECLARE
    v_rel  TEXT;
    v_role TEXT;
BEGIN
    FOREACH v_rel IN ARRAY ARRAY['extensions.pg_stat_statements', 'extensions.pg_stat_statements_info'] LOOP
        IF to_regclass(v_rel) IS NULL THEN
            CONTINUE;
        END IF;
        BEGIN
            EXECUTE format('REVOKE SELECT ON %s FROM anon, authenticated', v_rel);
        EXCEPTION WHEN insufficient_privilege THEN
            RAISE WARNING '057: % may not revoke SELECT on % from the request roles (the extension owner''s setting, docs/KNOWN-ISSUES.md)',
                current_user, v_rel;
        END;
        FOREACH v_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
            IF has_table_privilege(v_role, v_rel, 'SELECT') THEN
                RAISE WARNING '057: % still reads % (through PUBLIC, the extension''s own grant, or a grant % did not make; schema extensions is not exposed by PostgREST)',
                    v_role, v_rel, current_user;
            END IF;
        END LOOP;
    END LOOP;
END
$$;

-- 8. The self-check.
DO $$
DECLARE
    v_role  TEXT;
    v_priv  TEXT;
    v_rec   RECORD;
    v_owner NAME;
    v_n     INT;
BEGIN
    PERFORM set_config('search_path', '', true);

    -- (a) The function allowlist: every entry exists, is SECURITY DEFINER with a
    --     fixed search_path, and is executable by exactly its roles; nothing else
    --     is executable by a request role or by PUBLIC.
    FOR v_rec IN SELECT fn FROM pg_temp._057_exec_allow LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_proc WHERE oid = to_regprocedure(v_rec.fn) AND prosecdef
                          AND EXISTS (SELECT 1 FROM unnest(proconfig) c WHERE c LIKE 'search_path=%')) THEN
            RAISE EXCEPTION '057: allowlisted function % is missing or not SECURITY DEFINER with a fixed search_path', v_rec.fn;
        END IF;
    END LOOP;
    FOR v_rec IN
        SELECT p.oid::regprocedure AS fn, r.rolname AS role,
               has_function_privilege(r.rolname, p.oid, 'EXECUTE') AS held,
               EXISTS (SELECT 1 FROM pg_temp._057_exec_allow l
                        WHERE l.fn = p.oid::regprocedure::text AND r.rolname = ANY (l.roles)) AS allowed
          FROM pg_proc p
          CROSS JOIN (VALUES ('anon'), ('authenticated')) AS r (rolname)
         WHERE p.pronamespace = 'public'::regnamespace
    LOOP
        IF v_rec.held AND NOT v_rec.allowed THEN
            RAISE EXCEPTION '057: % may execute %, which is not on the allowlist', v_rec.role, v_rec.fn;
        END IF;
        IF v_rec.allowed AND NOT v_rec.held THEN
            RAISE EXCEPTION '057: % lost EXECUTE on allowlisted %', v_rec.role, v_rec.fn;
        END IF;
    END LOOP;
    IF EXISTS (SELECT 1 FROM pg_proc p, aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a
                WHERE p.pronamespace = 'public'::regnamespace AND a.grantee = 0) THEN
        RAISE EXCEPTION '057: PUBLIC still holds EXECUTE on a function in public';
    END IF;

    -- (b) No table-level privilege of PUBLIC or a request role on any relation
    --     (aclexplode reads every privilege, MAINTAIN included) …
    SELECT count(*) INTO v_n
      FROM pg_class c, aclexplode(c.relacl) a
     WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
       AND a.grantee IN (0, to_regrole('anon'), to_regrole('authenticated'));
    IF v_n > 0 THEN
        RAISE EXCEPTION '057: % table- or sequence-level privilege(s) of PUBLIC or a request role remain', v_n;
    END IF;
    --     … no column-level write, and column SELECT exactly the read allowlist.
    FOR v_rec IN
        SELECT c.oid::regclass AS rel, a.attname AS col, r.rolname AS role,
               has_column_privilege(r.rolname, c.oid, a.attnum, 'SELECT') AS can_read,
               (has_column_privilege(r.rolname, c.oid, a.attnum, 'INSERT')
                OR has_column_privilege(r.rolname, c.oid, a.attnum, 'UPDATE')
                OR has_column_privilege(r.rolname, c.oid, a.attnum, 'REFERENCES')) AS can_write,
               EXISTS (SELECT 1 FROM pg_temp._057_read_allow l
                        WHERE to_regclass(l.rel) = c.oid AND r.rolname = ANY (l.roles)
                          AND a.attname = ANY (l.cols)) AS allowed
          FROM pg_class c
          JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
          CROSS JOIN (VALUES ('anon'), ('authenticated')) AS r (rolname)
         WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
    LOOP
        IF v_rec.can_write THEN
            RAISE EXCEPTION '057: % may write %.%', v_rec.role, v_rec.rel, v_rec.col;
        END IF;
        IF v_rec.can_read AND NOT v_rec.allowed THEN
            RAISE EXCEPTION '057: % may read %.%, which is not on the read allowlist', v_rec.role, v_rec.rel, v_rec.col;
        END IF;
        IF v_rec.allowed AND NOT v_rec.can_read THEN
            RAISE EXCEPTION '057: % may not read allowlisted %.%', v_rec.role, v_rec.rel, v_rec.col;
        END IF;
    END LOOP;
    IF EXISTS (SELECT 1 FROM pg_temp._057_read_allow l, unnest(l.cols) AS c
                WHERE to_regclass(l.rel) IS NULL
                   OR NOT EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = to_regclass(l.rel)
                                     AND a.attname = c AND a.attnum > 0 AND NOT a.attisdropped)) THEN
        RAISE EXCEPTION '057: the read allowlist names a table or column that does not exist';
    END IF;

    -- (c) A policy of a table a request role reads may call only functions that
    --     role executes, and sub-select only columns that role reads (a policy's
    --     sub-select runs with the reader's privileges; else RLS itself fails).
    FOR v_rec IN
        SELECT DISTINCT pol.polrelid::regclass AS rel, pol.polname, rr.rolname AS role,
               d.refclassid, d.refobjid, d.refobjsubid
          FROM pg_policy pol
          JOIN pg_depend d ON d.classid = 'pg_policy'::regclass AND d.objid = pol.oid
          CROSS JOIN (VALUES ('anon'), ('authenticated')) AS rr (rolname)
         WHERE pol.polrelid IN (SELECT oid FROM pg_class WHERE relnamespace = 'public'::regnamespace)
           AND pol.polcmd IN ('r', '*')
           AND (0::oid = ANY (pol.polroles) OR to_regrole(rr.rolname)::oid = ANY (pol.polroles))
           AND has_any_column_privilege(rr.rolname, pol.polrelid, 'SELECT')
    LOOP
        IF v_rec.refclassid = 'pg_proc'::regclass
           AND NOT has_function_privilege(v_rec.role, v_rec.refobjid, 'EXECUTE') THEN
            RAISE EXCEPTION '057: policy "%" on % calls %, which % may not execute (add it to the allowlist?)',
                v_rec.polname, v_rec.rel, v_rec.refobjid::regprocedure, v_rec.role;
        END IF;
        IF v_rec.refclassid = 'pg_class'::regclass AND v_rec.refobjid <> v_rec.rel AND v_rec.refobjsubid > 0
           AND NOT has_column_privilege(v_rec.role, v_rec.refobjid, v_rec.refobjsubid::smallint, 'SELECT') THEN
            RAISE EXCEPTION '057: policy "%" on % reads %.% (column %), which % may not read (add it to the read allowlist?)',
                v_rec.polname, v_rec.rel, v_rec.refobjid::regclass, v_rec.refobjsubid,
                (SELECT attname FROM pg_attribute WHERE attrelid = v_rec.refobjid AND attnum = v_rec.refobjsubid),
                v_rec.role;
        END IF;
    END LOOP;

    -- (d) No write policy is left for a request role on a public table.
    IF EXISTS (SELECT 1 FROM pg_policy pol
                WHERE pol.polrelid IN (SELECT oid FROM pg_class WHERE relnamespace = 'public'::regnamespace)
                  AND pol.polcmd <> 'r'
                  AND (0::oid = ANY (pol.polroles) OR to_regrole('anon')::oid = ANY (pol.polroles)
                       OR to_regrole('authenticated')::oid = ANY (pol.polroles))) THEN
        RAISE EXCEPTION '057: a public table still carries a write policy for a request role';
    END IF;

    -- (e) Every service_role privilege is as it was before this migration.
    SELECT count(*) INTO v_n FROM pg_temp._057_service_before b
     WHERE (b.kind = 'table' AND b.held IS DISTINCT FROM has_table_privilege('service_role', b.obj, b.priv))
        OR (b.kind = 'sequence' AND b.held IS DISTINCT FROM has_sequence_privilege('service_role', b.obj, b.priv))
        OR (b.kind = 'function' AND b.held IS DISTINCT FROM has_function_privilege('service_role', b.obj, b.priv));
    IF v_n > 0 THEN
        RAISE EXCEPTION '057: % service_role privileges changed', v_n;
    END IF;

    -- (f) Default privileges: no entry in public or global grants a request role
    --     or PUBLIC anything, for every role whose defaults could be changed …
    FOR v_owner IN
        SELECT DISTINCT r.rolname FROM pg_default_acl d JOIN pg_roles r ON r.oid = d.defaclrole
         WHERE d.defaclnamespace IN (0, 'public'::regnamespace)
    LOOP
        IF NOT pg_has_role(current_user, v_owner, 'USAGE') THEN
            CONTINUE;
        END IF;
        IF EXISTS (SELECT 1 FROM pg_default_acl d, aclexplode(d.defaclacl) a
                    WHERE d.defaclrole = to_regrole(v_owner)
                      AND d.defaclnamespace IN (0, 'public'::regnamespace)
                      AND d.defaclobjtype IN ('r', 'S', 'f')
                      AND (a.grantee = 0 OR a.grantee IN (to_regrole('anon'), to_regrole('authenticated')))) THEN
            RAISE EXCEPTION '057: a default privilege of % still grants a request role or PUBLIC', v_owner;
        END IF;
        -- … and the built-in PUBLIC EXECUTE on functions is gone (a global entry exists).
        IF NOT EXISTS (SELECT 1 FROM pg_default_acl d
                        WHERE d.defaclrole = to_regrole(v_owner) AND d.defaclnamespace = 0
                          AND d.defaclobjtype = 'f') THEN
            RAISE EXCEPTION '057: % has no global function default; PostgreSQL''s PUBLIC EXECUTE still applies', v_owner;
        END IF;
    END LOOP;
    --     And by behaviour: what this role creates in public now is closed to the
    --     request roles and open to service_role.
    EXECUTE 'CREATE TABLE public._057_probe (id bigint GENERATED BY DEFAULT AS IDENTITY, v text)';
    EXECUTE 'CREATE SEQUENCE public._057_probe_seq';
    EXECUTE 'CREATE FUNCTION public._057_probe_fn() RETURNS integer LANGUAGE sql AS ''SELECT 1''';
    FOREACH v_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
        IF has_any_column_privilege(v_role, 'public._057_probe', 'SELECT')
           OR EXISTS (SELECT 1 FROM pg_class c, aclexplode(c.relacl) a
                       WHERE c.oid IN ('public._057_probe'::regclass, 'public._057_probe_seq'::regclass,
                                       pg_get_serial_sequence('public._057_probe', 'id')::regclass)
                         AND a.grantee IN (0, to_regrole(v_role))) THEN
            RAISE EXCEPTION '057: a new table or sequence grants % a privilege', v_role;
        END IF;
        IF has_function_privilege(v_role, 'public._057_probe_fn()', 'EXECUTE') THEN
            RAISE EXCEPTION '057: a new function is executable by %', v_role;
        END IF;
    END LOOP;
    FOREACH v_priv IN ARRAY ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE'] LOOP
        IF NOT has_table_privilege('service_role', 'public._057_probe', v_priv) THEN
            RAISE EXCEPTION '057: a new table no longer grants % to service_role', v_priv;
        END IF;
    END LOOP;
    IF NOT has_sequence_privilege('service_role', 'public._057_probe_seq', 'USAGE')
       OR NOT has_function_privilege('service_role', 'public._057_probe_fn()', 'EXECUTE') THEN
        RAISE EXCEPTION '057: a new sequence or function is closed to service_role';
    END IF;
    EXECUTE 'DROP FUNCTION public._057_probe_fn()';
    EXECUTE 'DROP SEQUENCE public._057_probe_seq';
    EXECUTE 'DROP TABLE public._057_probe';

    -- (g) Storage: no policy for a request role, no public bucket.
    IF to_regclass('storage.objects') IS NOT NULL
       AND EXISTS (SELECT 1 FROM pg_policy pol
                    WHERE pol.polrelid IN (SELECT oid FROM pg_class
                                            WHERE relnamespace = 'storage'::regnamespace)
                      AND (0::oid = ANY (pol.polroles) OR to_regrole('anon')::oid = ANY (pol.polroles)
                           OR to_regrole('authenticated')::oid = ANY (pol.polroles))) THEN
        RAISE EXCEPTION '057: a storage policy applies to a request role';
    END IF;
    IF to_regclass('storage.buckets') IS NOT NULL THEN
        EXECUTE 'SELECT count(*) FROM storage.buckets WHERE public' INTO v_n;
        IF v_n > 0 THEN
            RAISE EXCEPTION '057: % public storage bucket(s) remain', v_n;
        END IF;
    END IF;

    -- (h) Realtime still publishes trainings (the student's live chart).
    IF EXISTS (SELECT 1 FROM pg_publication WHERE pubname = 'supabase_realtime')
       AND NOT EXISTS (SELECT 1 FROM pg_publication_tables
                        WHERE pubname = 'supabase_realtime' AND schemaname = 'public' AND tablename = 'trainings') THEN
        RAISE EXCEPTION '057: trainings left the supabase_realtime publication';
    END IF;

    RAISE NOTICE '057 self-check passed';
END
$$;

COMMIT;
