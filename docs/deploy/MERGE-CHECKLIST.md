# Before merging to `main`

**Merging to `main` is a production deploy.** Three workflows fire on a push to
`main`, each by its own path filter, in no fixed order, within minutes, and
without a tag:

| Workflow | Fires on a change under | Deploys |
|---|---|---|
| W1 `supabase-migrate.yml` | `robotis_ai_setup/supabase/migrations/` | every new migration, to the production database |
| W2 `railway-deploy-cloud-api.yml` | `robotis_ai_setup/cloud_training_api/` | the cloud API |
| W3 `railway-deploy-teacher-web.yml` | `physical_ai_tools/physical_ai_manager/` | the teacher web |

W4 (`docker-publish.yml`) also builds `:latest` images on a merge; both fleets
pin a release tag, so no robot pulls them. The robots, the Windows GUI and the
Pi agent change only with a release tag (`release.yml`).

Every step below is an owner action (CLAUDE.md, „When to ask the user"). The
details sit in `DEPLOY.md` §3c and §3d, which are current despite that file's
banner.

## The launch-plan branch (steps 2, 3, 4, 5, 8, 12 and the wave-A fixes)

### 1. Before the merge, in this order

1. **The project's CURRENT signing key is the ES256 key** (`DEPLOY.md` §3c
   step 1). The new API accepts only ES256/RS256 tokens whose key the JWKS
   lists: with the legacy HS256 secret still current, every request answers 401
   the moment W2 is live. Rotate first, or set `AUTH_VERIFY_MODE=remote` on
   Railway (`--skip-deploys`) before merging and rotate later.
2. **TOTP is enabled** in the Supabase dashboard (§3c step 2). Without it the
   admin's enrolment screen says the second factor is not available, and the
   admin cannot reach the admin area at all.
3. **The project's password minimum is 8** (§3c step 3).
4. **Step 4's variables are decided** (§3d). The three `LEGAL_*_URL` on the API
   service are optional: set them only when the pages exist, and only with
   valid `https://` values — an invalid one stops the new API's boot, its
   `/health` gate fails and the old API keeps serving. `EDUBOTICS_CSP_ENFORCE`
   on the teacher-web service stays UNSET (report-only) for the first week.
   Step 5 adds no variable.
5. **The admin is ready to enrol right after W3** (section 3 below): the first
   enrolment wins.

### 2. What the merge deploys, and the windows between the three

- **W1** applies migrations 055 (`public.users` default-deny) and 056 (an
  admin's RLS reads need a two-factor session), and 054 if it is not applied
  yet. Each checks itself and changes nothing when the check fails (the W1 log
  shows „055 self-check passed" / „056 self-check passed"). Both are safe in
  either order with W2: the API writes and reads with the service role, and the
  teacher web reads nothing from the database directly.
- **W2** deploys the hardened API: local token check, the admin's `aal2` gate,
  the failed-authentication cap, the password rule, the deletion-request routes,
  the legal links on `/version`.
- **W3** deploys the teacher web with the admin's TOTP screen (`AdminMfaGate`).
- **The W2 → W3 window:** when W2 is live before W3, every admin route answers
  403 `mfa_required` to an admin page that has no TOTP screen yet, so the admin
  dashboard shows errors until W3's `/version.json` gate has passed (minutes).
  Teachers and students are not affected. Do not use the admin dashboard in that
  window. (W3 before W2 is harmless: the older API accepts the `aal2` token the
  new screen produces.)

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

### 4. After the merge

- §3c steps 4 and 5: the „JWKS: 1 signing key(s) cached" log line, the 401
  `curl`, the client-address check (it decides `TRUSTED_PROXY_HOPS`).
- In the SQL editor: `SELECT has_table_privilege('authenticated',
  'public.users', 'UPDATE')` answers `false` (055), and
  `SELECT pg_get_functiondef('public.rls_is_admin()'::regprocedure)` names
  `aal2` (056).
- Tell the teachers about „Löschanfragen" (`DEPLOY.md`, migration 055).
- One week later, the CSP decision of §3d.

### 5. Rollback

- The API's token check: `AUTH_VERIFY_MODE=remote` (no code rollback).
- 056: `supabase/rollback/20261112120000_056_admin_requires_aal2_rollback.sql`;
  it re-opens the password-only admin read and needs no API rollback.
- 055: its rollback, as `DEPLOY.md` describes.
- The API or the teacher web: redeploy the previous commit; the older API
  accepts an `aal2` token and works on a database with 055 and 056.
