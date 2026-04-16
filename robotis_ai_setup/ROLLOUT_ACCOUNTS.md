# Rollout: Teacher / Classroom / Student Account System

This document covers the post-merge steps needed to ship the new account system
to production. The code is done; these are the manual ops actions.

## Status summary

| Step | Status | Where |
|---|---|---|
| `002_accounts.sql` migration | Applied to prod Supabase | `supabase/002_accounts.sql` |
| Cloud API deployed to Railway | Deployed | `cloud_training_api/` |
| Web deployment config added | Ready | `physical_ai_tools/physical_ai_manager/vercel.json` |
| Admin user bootstrapped | Pending | see below |
| Supabase email signup disabled | Pending | see below |
| Vercel / static web deployment | Pending | see below |
| Docker image rebuild | Pending | `nettername/physical-ai-manager` |

## 1. Bootstrap your admin account

Run once, locally:

```bash
cd robotis_ai_setup
# Assumes cloud_training_api/.env has SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY
pip install supabase python-dotenv  # one-time if not already
python scripts/bootstrap_admin.py --username admin --full-name "Sven"
# Enter password when prompted.
```

This creates `admin@edubotics.local` in auth.users and sets `role='admin'` in
public.users. Use the username `admin` (without the @-domain) to log in to the
web dashboard.

Verify:

```sql
-- In Supabase SQL editor
SELECT id, username, role, full_name FROM public.users WHERE role = 'admin';
```

## 2. Disable email signup in Supabase

In the Supabase dashboard:

1. Project `fnnbysrjkfugsqzwcksd` → Authentication → Providers → Email
2. Toggle **Enable signups** → OFF
3. Save

This blocks `supabase.auth.signUp` at the infrastructure level — defence in
depth against any old clients that still have the registration form.

## 3. Deploy the web dashboard (teachers + admin)

Pick any static host. The config is already in
`physical_ai_tools/physical_ai_manager/vercel.json`. For Vercel:

```bash
cd physical_ai_tools/physical_ai_manager
vercel link                       # first time only
vercel --prod
# Or connect the repo in the Vercel UI and set these env vars:
#   REACT_APP_MODE               web
#   REACT_APP_SUPABASE_URL       https://fnnbysrjkfugsqzwcksd.supabase.co
#   REACT_APP_SUPABASE_ANON_KEY  eyJ...  (anon key from Supabase dashboard)
#   REACT_APP_CLOUD_API_URL      https://scintillating-empathy-production-9efd.up.railway.app
```

After deploy, add the Vercel URL to the Cloud API's `ALLOWED_ORIGINS` in
Railway (Variables tab), then redeploy:

```
ALLOWED_ORIGINS=http://localhost,http://localhost:80,https://YOUR-VERCEL-URL.vercel.app
```

## 4. Rebuild the student Docker image

The `physical_ai_manager` Dockerfile now defaults to `REACT_APP_MODE=student`,
so the existing build process still works. Just rebuild and push:

```bash
cd robotis_ai_setup/docker
REGISTRY=nettername \
  SUPABASE_URL=https://fnnbysrjkfugsqzwcksd.supabase.co \
  SUPABASE_ANON_KEY=eyJ... \
  CLOUD_API_URL=https://scintillating-empathy-production-9efd.up.railway.app \
  ./build-images.sh

docker push nettername/physical-ai-manager:latest
```

Students get the new build on next `docker compose pull`.

## 5. First smoke test (once everything is up)

1. Open the Vercel URL → log in with your admin username + password
2. Admin dashboard → Neuer Lehrer → create a test teacher with some credits
3. Log out, log in as the new teacher
4. Create a classroom, then create a student with some credits
5. On a student Docker machine (or dev mode), log in with the student's
   username → Training page should show `N / N Trainingsguthaben`
6. Start a training → verify credit decrements in teacher dashboard

## API smoke tests (curl)

With a teacher JWT (grab from `localStorage` after logging in):

```bash
# Who am I?
curl -s https://scintillating-empathy-production-9efd.up.railway.app/me \
  -H "Authorization: Bearer $JWT" | jq

# Create a classroom
curl -s https://.../teacher/classrooms \
  -H "Authorization: Bearer $JWT" -H "Content-Type: application/json" \
  -d '{"name":"Klasse 8A"}' | jq

# Create a student in that classroom
curl -s https://.../teacher/classrooms/<CLASSROOM_ID>/students \
  -H "Authorization: Bearer $JWT" -H "Content-Type: application/json" \
  -d '{"username":"test.student","password":"abc123","full_name":"Test Student","initial_credits":3}' | jq

# Adjust credits
curl -s https://.../teacher/students/<STUDENT_ID>/credits \
  -H "Authorization: Bearer $JWT" -H "Content-Type: application/json" \
  -d '{"delta":2}' | jq
```

## What's *not* touched

- Existing `/trainings/*` endpoints and RPCs (`start_training_safe`,
  `get_remaining_credits`, `update_training_progress`) are unchanged. Student
  training flow is identical.
- The existing test student row (`teststudent@robotis-ai.test`) still has its 5
  credits. It now has `role='student'` and no `classroom_id`, so the teacher
  dashboard won't see it. You can delete it or assign it to a test classroom
  later:

```sql
-- To delete:
DELETE FROM auth.users WHERE email = 'teststudent@robotis-ai.test';
-- Cascade deletes from public.users.
```
