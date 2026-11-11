-- Supabase stand-ins for replaying robotis_ai_setup/supabase/migrations into a
-- plain PostgreSQL 16 (ci.yml::migrations-replay, and by hand on a local
-- cluster). Run ONCE, as a superuser, on a fresh database, before the first
-- migration. Never against the linked project.
--
-- What it stands in for, and why each piece is here:
--   * the request roles anon / authenticated, service_role (BYPASSRLS), the
--     PostgREST login role authenticator, and the platform roles that own
--     Supabase's own schemas (supabase_admin, supabase_auth_admin,
--     supabase_storage_admin);
--   * auth.users plus auth.uid() / auth.role() / auth.jwt() reading the JWT
--     claims the way PostgREST sets them (request.jwt.claims), so an
--     assertion file can act as a student with SET LOCAL ROLE + set_config;
--   * a minimal storage schema (buckets, objects with RLS) and the production
--     DRIFT read from the live catalog on 2026-11-10 (security audit SA-01): a
--     PUBLIC bucket `releases` and an INSERT policy "Service role upload to
--     releases" for every signed-in user. No migration created them; 057
--     removes the policy and makes the bucket private, and the replay check
--     proves it;
--   * extensions.pg_stat_statements(_info) with Supabase's SELECT grant to the
--     request roles (SA-22), when the server ships the extension;
--   * the supabase_realtime publication;
--   * Supabase's default privileges: every table, function and sequence a
--     migration creates in public is granted ALL to anon, authenticated and
--     service_role (FOR ROLE postgres and FOR ROLE supabase_admin, as the live
--     pg_default_acl reads). This is the root cause 057 removes (SA-10).
--
-- Idempotent for the roles (a cluster keeps them across databases).

DO $$
DECLARE
    v_role TEXT;
BEGIN
    FOREACH v_role IN ARRAY ARRAY['anon', 'authenticated', 'supabase_auth_admin', 'supabase_storage_admin'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = v_role) THEN
            EXECUTE format('CREATE ROLE %I NOLOGIN NOINHERIT', v_role);
        END IF;
    END LOOP;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        CREATE ROLE service_role NOLOGIN NOINHERIT BYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticator') THEN
        CREATE ROLE authenticator LOGIN NOINHERIT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'supabase_admin') THEN
        CREATE ROLE supabase_admin LOGIN SUPERUSER;
    END IF;
END
$$;
GRANT anon, authenticated, service_role TO authenticator;

-- auth -----------------------------------------------------------------------
CREATE SCHEMA auth AUTHORIZATION supabase_auth_admin;
CREATE TABLE auth.users (
    id uuid PRIMARY KEY,
    instance_id uuid,
    aud varchar(255),
    role varchar(255),
    email varchar(255),
    encrypted_password varchar(255),
    created_at timestamptz DEFAULT now(),
    updated_at timestamptz,
    raw_user_meta_data jsonb,
    raw_app_meta_data jsonb,
    is_anonymous boolean NOT NULL DEFAULT false
);
ALTER TABLE auth.users OWNER TO supabase_auth_admin;
CREATE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql STABLE AS $$
  SELECT coalesce(nullif(current_setting('request.jwt.claim.sub', true), ''),
                  (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub'))::uuid
$$;
CREATE FUNCTION auth.role() RETURNS text LANGUAGE sql STABLE AS $$
  SELECT coalesce(nullif(current_setting('request.jwt.claim.role', true), ''),
                  (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'role'))::text
$$;
CREATE FUNCTION auth.jwt() RETURNS jsonb LANGUAGE sql STABLE AS $$
  SELECT coalesce(nullif(current_setting('request.jwt.claims', true), ''), '{}')::jsonb
$$;
GRANT USAGE ON SCHEMA auth TO anon, authenticated, service_role;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA auth TO anon, authenticated, service_role;
-- The migrations put a trigger on auth.users and read it (as postgres).
GRANT ALL ON auth.users TO postgres;

-- storage --------------------------------------------------------------------
CREATE SCHEMA storage AUTHORIZATION supabase_storage_admin;
CREATE TABLE storage.buckets (
    id text PRIMARY KEY,
    name text NOT NULL,
    owner uuid,
    public boolean DEFAULT false,
    file_size_limit bigint,
    allowed_mime_types text[],
    created_at timestamptz DEFAULT now(),
    updated_at timestamptz DEFAULT now()
);
CREATE TABLE storage.objects (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    bucket_id text REFERENCES storage.buckets (id),
    name text,
    owner uuid,
    created_at timestamptz DEFAULT now(),
    metadata jsonb
);
ALTER TABLE storage.buckets ENABLE ROW LEVEL SECURITY;
ALTER TABLE storage.objects ENABLE ROW LEVEL SECURITY;
GRANT USAGE ON SCHEMA storage TO anon, authenticated, service_role;
GRANT ALL ON storage.buckets, storage.objects TO anon, authenticated, service_role;
-- The live drift (SA-01).
INSERT INTO storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
VALUES ('releases', 'releases', true, 314572800,
        ARRAY['application/octet-stream', 'application/x-msdownload', 'application/x-msdos-program']);
CREATE POLICY "Service role upload to releases" ON storage.objects
    FOR INSERT TO authenticated WITH CHECK (bucket_id = 'releases');
ALTER TABLE storage.buckets OWNER TO supabase_storage_admin;
ALTER TABLE storage.objects OWNER TO supabase_storage_admin;

-- extensions -------------------------------------------------------------------
CREATE SCHEMA extensions;
GRANT USAGE ON SCHEMA extensions TO anon, authenticated, service_role;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pg_stat_statements') THEN
        CREATE EXTENSION pg_stat_statements WITH SCHEMA extensions;
        GRANT SELECT ON extensions.pg_stat_statements, extensions.pg_stat_statements_info
            TO anon, authenticated;
    END IF;
END
$$;

-- realtime ---------------------------------------------------------------------
CREATE PUBLICATION supabase_realtime;

-- public: Supabase's grants and default privileges ------------------------------
GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
    GRANT ALL ON TABLES TO anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
    GRANT ALL ON FUNCTIONS TO anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
    GRANT ALL ON SEQUENCES TO anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public
    GRANT ALL ON TABLES TO anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public
    GRANT ALL ON FUNCTIONS TO anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public
    GRANT ALL ON SEQUENCES TO anon, authenticated, service_role;
