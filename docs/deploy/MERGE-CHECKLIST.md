# Before merging to `main`

**Merging to `main` is a production deploy, but not of the database.** Two
workflows fire on a push to `main`, each by its own path filter, in no fixed
order, within minutes, and without a tag:

| Workflow | Fires on a change under | Deploys |
|---|---|---|
| W2 `railway-deploy-cloud-api.yml` | `robotis_ai_setup/cloud_training_api/` | the cloud API |
| W3 `railway-deploy-teacher-web.yml` | `physical_ai_tools/physical_ai_manager/` | the teacher web |

**W1 `supabase-migrate.yml` does NOT fire on a merge** (owner decision
2026-11-15). The production database changes only through a release
(`release.yml` calls W1 first, on a tag or when `release.yml` itself is
dispatched by hand) or by hand: GitHub → Actions → **supabase-migrate** → **Run
workflow**, target `production` (any other target fails the run and changes
nothing, a rollback included). Why: GitHub's required reviewers for an environment, the
SA-13 approval gate, exist on a PRIVATE repository only with GitHub Enterprise
(Free, Pro and Team: public repositories only), so `environment: production`
cannot hold a merge's migration on this plan, and the merge used to fire W1, W2
and W3 at once, in no order (verification round B, VB-02).

W4 (`docker-publish.yml`) also builds `:latest` images on a merge; both fleets
pin a release tag, so no robot pulls them. The robots, the Windows GUI and the
Pi agent change only with a release tag (`release.yml`).

Every step below is an owner action (CLAUDE.md, „When to ask the user"). The
details sit in `DEPLOY.md` §1 (053 … 059), §3c and §3d.

## This merge: the launch-plan branch and the deep security audit (migrations 053 … 059)

What it brings, measured by verification round B (2026-11-15):

- **The production database is at 052.** The merge brings seven migrations:
  053 (the classroom Jetson's objects dropped), 054 (advisor clean-up), 055
  (`public.users` default-deny), 056 (an admin's RLS reads need `aal2`), 057
  (the request roles default-deny; the `releases` storage locked), 058 (no
  account the server did not create) and 059 (reserved Hugging Face namespaces).
- **The new API runs on a 052 database**: it booted with `schema_ok: true`, and
  `/me`, `/trainings/quota`, `/trainings/list`, `/datasets`, `/workflows`, the
  deletion requests, the export, the teacher classrooms and the admin routes all
  answered 200.
- **Production's current API does NOT run on a 053 database**: its boot probe
  asks for `public.jetsons`, and it exits at start-up („Schema probe failed at
  startup — Cloud API exiting"). Railway may restart it at any time.
- **058 needs the new API**: the older API's account creation sends no
  provisioning marker, so after 058 every „Schüler anlegen" and every new
  teacher answers 500 until the new API serves.

Hence the order: **merge (W2 and W3 deploy) → the new API's `/health` → W1 by
hand → the live checks.** The database moves only after the API that can run on
both schemas serves, so there is neither a crash window for the old API nor a
500 window for account creation.

### 1. Before the merge, in this order

1. **The project's CURRENT signing key is the ES256 key** (`DEPLOY.md` §3c
   step 1). The new API accepts only ES256/RS256 tokens whose key the JWKS
   lists: with the legacy HS256 secret still current, every request answers 401
   the moment W2 is live. Rotate first, or set `AUTH_VERIFY_MODE=remote` on
   Railway (`--skip-deploys`) before merging and rotate later.
2. **TOTP is enabled** for the admin's second factor (§3c step 2). Without it the
   admin's enrolment screen says the second factor is not available, and the
   admin cannot reach the admin area at all.
3. **The project's password minimum is 8** (§3c step 3).
4. **Sign-up is off** (SA-03): Supabase dashboard → Authentication → Sign In /
   Providers → „Allow new users to sign up" off, and auto-confirm off. 058 is
   the database's floor behind that switch.
5. **Accounts that registered themselves are deleted.** Sign-up was open with
   auto-confirm, and until 044 (`role`) and 055 (`created_by`, `username`, …)
   an account could write its own `public.users` row through PostgREST, so
   those columns prove nothing about who made an account (verification round C,
   VC-02). The two queries below lean on what an account can NEVER write: Supabase
   Auth's own audit log (`auth.audit_log_entries`: the server's
   `auth.admin.create_user` is logged as `user_signedup` with the actor
   `service_role`; a sign-up through `/signup` with the new user itself as the
   actor) and the server's login e-mail `<username>@edubotics.local`, and they
   list every admin and teacher BY NAME, because a self-made admin or teacher is
   the dangerous one. An account invited from the dashboard („Invite user") is
   no self-registration although its acceptance is logged with the invitee as
   the actor: only an admin can invite, and the invitation itself is logged as
   `user_invited` (verification round D, VD-02), so both queries list it as
   `INVITED from the dashboard` for you to recognise, never as proof. Read-only, in the SQL editor. First, does the audit log
   reach back far enough to help (it may be switched off for the database or
   pruned)?

   ```sql
   SELECT count(*) AS entries, min(created_at) AS oldest FROM auth.audit_log_entries;
   ```

   (If the editor answers `permission denied` for `audit_log_entries`, write
   `false` in place of each `EXISTS (SELECT 1 FROM auth.audit_log_entries …)`
   below: the queries then judge as for an empty audit log.)

   Then (a) every admin and every teacher — recognise each one by name; anyone
   you do not know, or `origin` = `SIGNED ITSELF UP`, is deleted:

   ```sql
   SELECT u.role, u.username, u.full_name, a.email, a.created_at, a.last_sign_in_at,
          creator.username AS created_by,
          CASE
            WHEN EXISTS (SELECT 1 FROM auth.audit_log_entries l
                         WHERE l.payload->>'action' = 'user_invited'
                           AND l.payload->'traits'->>'user_id' = a.id::text)
              THEN 'INVITED from the dashboard'
            WHEN EXISTS (SELECT 1 FROM auth.audit_log_entries l
                         WHERE l.payload->>'actor_id' = a.id::text
                           AND l.payload->>'action' IN ('user_signedup', 'user_confirmation_requested'))
              THEN 'SIGNED ITSELF UP'
            WHEN EXISTS (SELECT 1 FROM auth.audit_log_entries l
                         WHERE l.payload->>'action' = 'user_signedup'
                           AND l.payload->>'actor_username' = 'service_role'
                           AND l.payload->'traits'->>'user_id' = a.id::text)
              THEN 'created by the server'
            ELSE 'not in the audit log'
          END AS origin
   FROM public.users u
   JOIN auth.users a ON a.id = u.id
   LEFT JOIN public.users creator ON creator.id = u.created_by
   WHERE u.role::text IN ('admin', 'teacher')
   ORDER BY u.role, a.created_at;
   ```

   and (b) every other account whose origin the database cannot vouch for, with
   the reasons in `why`:

   ```sql
   WITH acct AS (
     SELECT a.id, a.email, a.created_at, a.last_sign_in_at,
            u.id AS profile, u.role::text AS role, u.username, u.created_by,
            creator.role::text AS creator_role, cl.teacher_id AS classroom_teacher,
            EXISTS (SELECT 1 FROM auth.audit_log_entries l
                    WHERE l.payload->>'action' = 'user_invited'
                      AND l.payload->'traits'->>'user_id' = a.id::text)
              AS invited,
            EXISTS (SELECT 1 FROM auth.audit_log_entries l
                    WHERE l.payload->>'actor_id' = a.id::text
                      AND l.payload->>'action' IN ('user_signedup', 'user_confirmation_requested'))
              AS signed_up_as_actor,
            EXISTS (SELECT 1 FROM auth.audit_log_entries l
                    WHERE l.payload->>'action' = 'user_signedup'
                      AND l.payload->>'actor_username' = 'service_role'
                      AND l.payload->'traits'->>'user_id' = a.id::text)
              AS server_created
     FROM auth.users a
     LEFT JOIN public.users u ON u.id = a.id
     LEFT JOIN public.users creator ON creator.id = u.created_by
     LEFT JOIN public.classrooms cl ON cl.id = u.classroom_id
   ), judged AS (
     SELECT *, signed_up_as_actor AND NOT invited AS signed_itself_up, concat_ws('; ',
       CASE WHEN invited THEN 'INVITED from the dashboard' END,
       CASE WHEN signed_up_as_actor AND NOT invited THEN 'SIGNED ITSELF UP (audit log)' END,
       CASE WHEN profile IS NULL THEN 'no public.users row' END,
       CASE WHEN email NOT LIKE '%@edubotics.local' THEN 'not an EduBotics login e-mail' END,
       CASE WHEN username IS DISTINCT FROM split_part(email, '@', 1) THEN 'username is not the login name' END,
       CASE WHEN created_by IS NULL OR created_by = id OR creator_role IS DISTINCT FROM 'teacher'
            THEN 'no teacher created it' END,
       CASE WHEN classroom_teacher IS DISTINCT FROM created_by
            THEN 'not in a classroom of the teacher who created it' END
     ) AS why
     FROM acct
     WHERE role IS NULL OR role NOT IN ('admin', 'teacher')
   )
   SELECT email, role, username, created_at, last_sign_in_at, server_created, why
   FROM judged
   WHERE signed_itself_up OR profile IS NULL OR (NOT server_created AND why <> '')
   ORDER BY created_at;
   ```

   `SIGNED ITSELF UP` is proof: delete it. `INVITED from the dashboard` is an
   account an admin invited: keep it if you know it, delete it if you do not.
   Any other row is a hint to review: an
   account the audit log proves the server made is never listed, so without the
   audit log a student an operator moved to another teacher's classroom shows
   up too („not in a classroom of the teacher who created it"). The one
   evasion neither query can see without the audit log is a sign-up that chose
   an `@edubotics.local` address AND wrote a real teacher's id and one of that
   teacher's classroom ids into its own row (ids only a classmate could have
   leaked): it then sits in that teacher's class list, so **ask every teacher to
   look over their class lists** for a student they did not create. Proven on a
   052 replay with the verifier's evasions (`docs/CLAUDE-CHANGELOG.md`,
   2026-11-15).

   Delete the ones nobody at the school created (Authentication → Users → the
   user → Delete). Accounts the server created before 058 carry no marker; that
   is harmless (058 judges only new accounts).
6. **The `releases` storage is closed** (SA-01): Supabase dashboard → Storage →
   Policies → delete the policy „Service role upload to releases"; then look
   through the bucket `releases` for anything you did not upload and delete the
   bucket (or make it private). 057 does both itself only where the migrating
   role may; on the hosted project `postgres` may not, and 057 then STOPS
   (`057: postgres may not drop the storage policy …`), W1 goes red after
   053 … 056 and 057, 058, 059 never land.
7. **`postgres` may read `auth.users`** (VB-07a): 058's check runs as its owner,
   `postgres` (the role W1's connection uses), and its self-check stops the
   migration when that role cannot read what it judges. Read-only, in the SQL
   editor:

   ```sql
   SELECT has_schema_privilege('postgres', 'auth', 'USAGE')                              AS auth_usage,
          has_column_privilege('postgres', 'auth.users', 'id', 'SELECT')                 AS id_select,
          has_column_privilege('postgres', 'auth.users', 'raw_app_meta_data', 'SELECT')  AS meta_select;
   ```

   All three must be `true`. If one is `false`, W1 would stop at 058 with
   053 … 057 applied (a state the new API runs on); do not run W1 until
   Supabase support has restored the grant, or stop and report.
8. **Step 4's variables are decided** (§3d). The three `LEGAL_*_URL` on the API
   service are optional: set them only when the pages exist, and only with valid
   `https://` values — an invalid one stops the new API's boot, its `/health`
   gate fails and the old API keeps serving. `EDUBOTICS_CSP_ENFORCE` on the
   teacher-web service stays UNSET (report-only) for the first week.
9. **The update channel has the pinned shape** (SA-01, VB-07b). The new
   `/version` serves an update only as a release asset of `GUI_RELEASE_REPO`.
   On the API service (Railway → the service → Variables; reading is enough):
   - `GUI_RELEASE_REPO` is exactly `owner/repo`;
   - `GUI_DOWNLOAD_URL` is unset, or exactly
     `https://github.com/<GUI_RELEASE_REPO>/releases/download/v<GUI_VERSION>/EduBotics_Setup.exe`
     (anything else is now ignored with one WARNING, and the URL is derived
     from `GUI_RELEASE_REPO` + `GUI_VERSION` instead — a URL into Supabase
     Storage, where the April installer still lies, is no longer served);
   - `GUI_INSTALLER_SHA256` is the SHA-256 of exactly that asset (a mismatch
     makes every updating GUI refuse the download).

   Save `curl -s https://scintillating-empathy-production-1068.up.railway.app/version`
   now, to compare after W2.
10. **The `production` environment** (GitHub → Settings → Environments, named
    exactly `production`, `DEPLOY.md` §0). It holds a job for approval only on a
    public repository or with GitHub Enterprise; on a private repository under
    Free, Pro or Team it is harmless and approves nothing — there the missing
    push trigger of W1 is the control.
11. **The admin is ready to enrol right after W3** (section 3 below): the first
    enrolment wins. Merge outside lessons: between W2 and W3 the admin pages
    answer errors (section 2).

### 2. The merge, then W1 by hand

1. **Merge.** W2 and W3 start, in no order between them. W1 does not start.
2. **Wait for W2 to go green**, then check by hand:

   ```bash
   curl -s https://scintillating-empathy-production-1068.up.railway.app/health
   ```

   `"commit"` must be the merge commit's SHA and `"schema_ok"` `true` (the new
   API on the 052 database). Compare `/version` with the copy from step 1.9.
   **If W2 is red, do NOT run W1**: the old API keeps serving (blue-green) on the
   untouched database; fix forward first.
3. **Wait for W3 to go green** (its `/version.json` gate), and enrol the admin's
   second factor at once (section 3).
4. **Run W1 by hand:** GitHub → Actions → **supabase-migrate** → **Run
   workflow** → branch `main`, target `production`, `rollback_to` EMPTY, reason
   e.g. „053–059, merge <short SHA>". Read the log of „Apply pending
   migrations": it applies 053 … 059 in order. When a migration stops, its own
   transaction changes nothing and the migrations before it stay applied (every
   intermediate state runs with the new API): fix the cause its message names
   and run W1 again — it applies only what is missing.

**The windows between W2 and W3, in either order** (minutes):

- W2 before W3: every admin route answers 403 `mfa_required` to the old admin
  page, which has no second-factor screen. Do not use the admin dashboard until
  W3 is green. Teachers and students are not affected.
- W3 before W2: the new teacher web's „Löschanfragen" queue and the legal links
  answer 404 from the old API until W2 is live. Nothing breaks; reload after W2.

### 3. Right after W3: enrol the admin's second factor — it is trust on first use

The first login with the admin PASSWORD after W3 shows the enrolment QR code and
binds whichever authenticator scans it. Until the real admin has enrolled,
anyone who knows the admin password can enrol THEIR authenticator first and so
own the account's second factor. Therefore:

1. Log in as every admin account at once after W3 and enrol (§3c step 6).
2. Supabase dashboard → Authentication → Users → each admin: exactly one MFA
   factor, created at the time you enrolled. An unexpected factor means the
   password is known to someone else: remove that factor, change the password,
   revoke the sessions, enrol again (§3c, „Recovery from a lost
   authenticator").

### 4. The live checks after W1

1. **The W1 log** names each migration's self-check: „054 self-check passed",
   „055 …", „056 …", „057 …", „058 …" and „059 self-check passed" (053 checks
   itself silently: it raises only on a failure). Expected besides: 057 may
   print a NOTICE that it may not change `supabase_admin`'s default privileges
   and a WARNING about `pg_stat_statements` (owner settings,
   `docs/KNOWN-ISSUES.md`); 059 prints how many forged `hf_username` values it
   cleared and WARNS when `datasets` rows already name a reserved namespace —
   review those rows (`docs/KNOWN-ISSUES.md` names the query).
2. **The students' reads through the live PostgREST and Realtime (057)**, with a
   student account on a robot or the cloud-only page: the Training page's live
   chart moves while a training runs (the trainings SELECT and its Realtime
   UPDATEs), a group member's new dataset appears in the dataset picker, the
   Roboter Studio template picker („Öffnen") lists the classroom's templates,
   and the Lernpfade progress loads. A read that broke names a column missing
   from `supabase/request_role_allowlist.json`: add it there with its proof, in
   a migration of its own.
3. **A few 42501 probes** with a student's access token (`$STUDENT`, from the
   browser's developer tools) and the project's anon key (`$ANON`); each
   answers an error whose `"code"` is `"42501"`:

   ```bash
   SB=https://fnnbysrjkfugsqzwcksd.supabase.co
   curl -s "$SB/rest/v1/users?select=email" -H "apikey: $ANON" -H "Authorization: Bearer $STUDENT"
   curl -s "$SB/rest/v1/trainings?select=training_params" -H "apikey: $ANON" -H "Authorization: Bearer $STUDENT"
   curl -s -X PATCH "$SB/rest/v1/users?id=eq.<the student's id>" -H "apikey: $ANON" \
        -H "Authorization: Bearer $STUDENT" -H "Content-Type: application/json" -d '{"full_name":"x"}'
   ```

   And in the SQL editor `SELECT has_table_privilege('authenticated',
   'public.users', 'UPDATE')` answers `false` (055);
   `SELECT pg_get_functiondef('public.rls_is_admin()'::regprocedure)` names
   `aal2` (056).
4. **A teacher creates a student** („Schüler anlegen") and the student logs in
   (058 with the new API's marker).
5. **An admin logs in with the second factor** and the admin dashboard loads; an
   admin session with only the password gets the TOTP screen, never data.
6. **§3c steps 4 and 5**: the „JWKS: 1 signing key(s) cached" log line, the 401
   `curl`, the client-address check (it decides `TRUSTED_PROXY_HOPS`).
7. **The Pi login on a real Pi** that runs this branch's agent and images (a dev
   Pi; the fielded Pis get them only with the release tag): claim with the
   setup code, a student logs in, rosbridge and the camera stream come up, the
   Startseite pushes the Hugging Face token (encrypted for the key the agent
   serves), a factory reset by a teacher shows the new code.
8. Tell the teachers about „Löschanfragen" (`DEPLOY.md`, migration 055). One
   week later, the CSP decision of §3d.

### 5. Rollback

- **The API's token check alone:** `AUTH_VERIFY_MODE=remote` on Railway (no code
  rollback).
- **The database, newest first, one migration at a time:** GitHub → Actions →
  **supabase-migrate** → **Run workflow** → `rollback_to` = the rollback file's
  name without `.sql`, in this order, as far as the incident needs:

  | Order | `rollback_to` | Re-opens |
  |---|---|---|
  | 1 | `20261113140000_059_hf_reserved_namespaces_rollback` | a reserved Hugging Face name |
  | 2 | `20261113130000_058_refuse_self_signup_rollback` | self-registration (the dashboard switch stays the fence) |
  | 3 | `20261113120000_057_request_role_default_deny_rollback` | the request roles' reads and writes |
  | 4 | `20261112120000_056_admin_requires_aal2_rollback` | the password-only admin read |
  | 5 | `20261111120000_055_users_identity_guard_rollback` | the student-writable `users` columns |
  | 6 | `20261110120000_054_advisor_cleanup_rollback` | the advisor findings |
  | 7 | `20261109120000_053_retire_jetson_rollback` | nothing (restores the Jetson schema, not its data) |

  The new API runs on every one of these states, so the database can go back
  while it serves.
- **The API, below this release: three rollbacks FIRST, in this order:
  059 → 058 → 053** (rows 1, 2 and 7 of the table; 059's must precede 058's).
  The previous API exits at start-up on a database with 053 (its probe asks for
  the Jetson objects), and under 058 its account creation answers 500. Then
  Railway → the API service → Deployments → redeploy the previous deployment.
  Never redeploy the previous API while 053 or 058 is applied. **054–057 may
  stay** and should (rolling them back re-opens SA-07, SA-09 and SA-10 for
  nothing): measured in verification round C (VC-08), the previous API's boot
  probe passes on 059/058/053 rolled back with 054–057 applied, and the
  previous student React and teacher web (release `f964555e`) read nothing
  057 closes — their one direct table read is the trainings `LIVE_COLUMNS`
  (all 22 granted, plus `user_id`), their Realtime channels (datasets,
  workflows, tutorial progress, trainings) are unchanged since and read only
  granted columns, and the old teacher web's one other channel, `jetsons`,
  restored by the 053 rollback without request-role grants, only ever
  triggered a refetch through the API and has no writer left (the Jetson is
  retired), so it stays silent as it would anyway.
- **The teacher web:** redeploy the previous one only together with the API: its
  admin pages have no second-factor screen, which the new API demands.
