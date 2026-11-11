#!/usr/bin/env python3
# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The request roles' privileges, judged on a replayed database (ci.yml::migrations-replay).

Replays every migration in robotis_ai_setup/supabase/migrations (timestamp
order, what `supabase db push` does) into a fresh database on a plain
PostgreSQL, after .github/scripts/supabase_replay_stub.sql (Supabase's roles,
auth/storage stand-ins, the live default privileges and storage drift), and
then FAILS on any of:

  * a table- or sequence-level privilege of PUBLIC, anon or authenticated on a
    public relation, a column-level write, or a column SELECT, outside
    robotis_ai_setup/supabase/request_role_allowlist.json ("reads", "tables",
    "sequences"); or an allowlisted read that is missing;
  * a request-role EXECUTE on a public function outside the allowlist
    ("functions"), a missing allowlisted one, or any PUBLIC EXECUTE;
  * a write policy (anything but SELECT) for a request role on a public table;
    a read policy of a table a request role reads that calls a function, or
    sub-selects a column, that role may not use;
  * a storage policy for a request role ("storage_policies") or a public bucket
    ("public_buckets");
  * a default privilege (schema public or global) granting a request role or
    PUBLIC anything, a role with a default ACL in public but no global function
    default (PostgreSQL's built-in PUBLIC EXECUTE), or — by behaviour — a probe
    table, sequence or function created by the migrating role being reachable
    by a request role.

The allowlist lives ONLY in the JSON; migration 057's self-check holds the same
lists and tests/test_supabase_057.py holds the two equal.

With --teeth-before PREFIX the check also runs once right before the first
migration whose file name starts at or after PREFIX and must FAIL there (the
state before 057: proof that the check can see the hole it guards).

Connection: the standard libpq environment (PGHOST, PGPORT, PGUSER,
PGPASSWORD) for a superuser; the database --db is dropped and re-created.
Stdlib only; needs `psql` on PATH.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / 'robotis_ai_setup' / 'supabase' / 'migrations'
ALLOWLIST = REPO / 'robotis_ai_setup' / 'supabase' / 'request_role_allowlist.json'
STUB = REPO / '.github' / 'scripts' / 'supabase_replay_stub.sql'
REQUEST_ROLES = ('anon', 'authenticated')
SEP = '\x1f'


def psql(db: str, sql: str | None = None, *, file: pathlib.Path | None = None) -> list[list[str]]:
    """Run SQL (or a file) with ON_ERROR_STOP; answer the rows of the output."""
    cmd = ['psql', '-X', '-q', '-At', '-F', SEP, '-v', 'ON_ERROR_STOP=1', '-d', db]
    cmd += ['-f', str(file)] if file else ['-c', sql]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f'psql failed ({file or sql[:80]}):\n{res.stderr.strip()}')
    return [line.split(SEP) for line in res.stdout.splitlines() if line]


def psql_script(db: str, script: str) -> list[list[str]]:
    res = subprocess.run(['psql', '-X', '-q', '-At', '-F', SEP, '-v', 'ON_ERROR_STOP=1', '-d', db],
                         input=script, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f'psql failed:\n{res.stderr.strip()}')
    return [line.split(SEP) for line in res.stdout.splitlines() if line]


# --- the catalog facts ------------------------------------------------------

def facts(db: str) -> dict:
    """Everything the judge needs, read with search_path '' (qualified names)."""
    q = lambda sql: psql(db, "SET search_path = ''; " + sql)  # noqa: E731
    out = {}
    out['relation_acl'] = q("""
        SELECT c.oid::regclass::text, c.relkind::text,
               CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE a.grantee::regrole::text END, a.privilege_type
          FROM pg_class c, aclexplode(c.relacl) a
         WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r','p','v','m','f','S')
           AND (a.grantee = 0 OR a.grantee::regrole::text IN ('anon','authenticated'))""")
    out['column_acl'] = q("""
        SELECT c.oid::regclass::text, a.attname::text, r.rolname, p.priv
          FROM pg_class c
          JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
          CROSS JOIN (VALUES ('anon'),('authenticated')) r (rolname)
          CROSS JOIN (VALUES ('SELECT'),('INSERT'),('UPDATE'),('REFERENCES')) p (priv)
         WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r','p','v','m','f')
           AND has_column_privilege(r.rolname, c.oid, a.attnum, p.priv)""")
    out['columns'] = q("""
        SELECT c.oid::regclass::text, a.attname::text
          FROM pg_class c JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
         WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r','p','v','m','f')""")
    out['function_exec'] = q("""
        SELECT p.oid::regprocedure::text, r.rolname
          FROM pg_proc p CROSS JOIN (VALUES ('anon'),('authenticated')) r (rolname)
         WHERE p.pronamespace = 'public'::regnamespace AND has_function_privilege(r.rolname, p.oid, 'EXECUTE')""")
    out['functions'] = [r[0] for r in q("""
        SELECT p.oid::regprocedure::text FROM pg_proc p WHERE p.pronamespace = 'public'::regnamespace""")]
    out['public_exec'] = [r[0] for r in q("""
        SELECT DISTINCT p.oid::regprocedure::text
          FROM pg_proc p, aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a
         WHERE p.pronamespace = 'public'::regnamespace AND a.grantee = 0""")]
    out['write_policies'] = q("""
        SELECT pol.polrelid::regclass::text, pol.polname::text, pol.polcmd::text
          FROM pg_policy pol
         WHERE pol.polrelid IN (SELECT oid FROM pg_class WHERE relnamespace = 'public'::regnamespace)
           AND pol.polcmd <> 'r'
           AND (0::oid = ANY (pol.polroles) OR to_regrole('anon')::oid = ANY (pol.polroles)
                OR to_regrole('authenticated')::oid = ANY (pol.polroles))""")
    out['policy_needs'] = q("""
        SELECT DISTINCT pol.polrelid::regclass::text, pol.polname::text, rr.rolname,
               CASE WHEN d.refclassid = 'pg_proc'::regclass THEN 'function' ELSE 'column' END,
               CASE WHEN d.refclassid = 'pg_proc'::regclass THEN d.refobjid::regprocedure::text
                    ELSE d.refobjid::regclass::text || '.' ||
                         (SELECT attname FROM pg_attribute WHERE attrelid = d.refobjid AND attnum = d.refobjsubid) END,
               CASE WHEN d.refclassid = 'pg_proc'::regclass THEN has_function_privilege(rr.rolname, d.refobjid, 'EXECUTE')
                    ELSE has_column_privilege(rr.rolname, d.refobjid, d.refobjsubid::smallint, 'SELECT') END::text
          FROM pg_policy pol
          JOIN pg_depend d ON d.classid = 'pg_policy'::regclass AND d.objid = pol.oid
          CROSS JOIN (VALUES ('anon'),('authenticated')) rr (rolname)
         WHERE pol.polrelid IN (SELECT oid FROM pg_class WHERE relnamespace = 'public'::regnamespace)
           AND pol.polcmd IN ('r','*')
           AND (0::oid = ANY (pol.polroles) OR to_regrole(rr.rolname)::oid = ANY (pol.polroles))
           AND has_any_column_privilege(rr.rolname, pol.polrelid, 'SELECT')
           AND (d.refclassid = 'pg_proc'::regclass
                OR (d.refclassid = 'pg_class'::regclass AND d.refobjid <> pol.polrelid AND d.refobjsubid > 0))""")
    has_storage = q("SELECT to_regclass('storage.objects') IS NOT NULL, to_regclass('storage.buckets') IS NOT NULL")
    out['storage_policies'] = q("""
        SELECT pol.polrelid::regclass::text, pol.polname::text
          FROM pg_policy pol
         WHERE pol.polrelid IN (SELECT oid FROM pg_class WHERE relnamespace = 'storage'::regnamespace)
           AND (0::oid = ANY (pol.polroles) OR to_regrole('anon')::oid = ANY (pol.polroles)
                OR to_regrole('authenticated')::oid = ANY (pol.polroles))""") if has_storage[0][0] == 't' else []
    out['public_buckets'] = [r[0] for r in q("SELECT id FROM storage.buckets WHERE public")] \
        if has_storage[0][1] == 't' else []
    out['default_acl'] = q("""
        SELECT d.defaclrole::regrole::text, coalesce(d.defaclnamespace::regnamespace::text, ''), d.defaclobjtype::text,
               CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE a.grantee::regrole::text END, a.privilege_type
          FROM pg_default_acl d, aclexplode(d.defaclacl) a
         WHERE d.defaclnamespace IN (0, 'public'::regnamespace)""")
    out['default_roles'] = [r[0] for r in q("""
        SELECT DISTINCT d.defaclrole::regrole::text FROM pg_default_acl d
         WHERE d.defaclnamespace = 'public'::regnamespace""")]
    out['global_function_default'] = [r[0] for r in q("""
        SELECT d.defaclrole::regrole::text FROM pg_default_acl d
         WHERE d.defaclnamespace = 0 AND d.defaclobjtype = 'f'""")]
    probe = psql_script(db, """
        BEGIN;
        CREATE TABLE public._ci_probe (id bigint GENERATED BY DEFAULT AS IDENTITY, v text);
        CREATE SEQUENCE public._ci_probe_seq;
        CREATE FUNCTION public._ci_probe_fn() RETURNS integer LANGUAGE sql AS 'SELECT 1';
        SELECT r.rolname,
               has_any_column_privilege(r.rolname, 'public._ci_probe', 'SELECT')::text,
               has_table_privilege(r.rolname, 'public._ci_probe', 'INSERT')::text,
               has_sequence_privilege(r.rolname, 'public._ci_probe_seq', 'USAGE')::text,
               has_function_privilege(r.rolname, 'public._ci_probe_fn()', 'EXECUTE')::text
          FROM (VALUES ('anon'), ('authenticated')) r (rolname);
        ROLLBACK;
        """)
    out['probe'] = probe
    return out


# --- the judge (pure: tests/test_supabase_request_role_check.py feeds it) ---

def judge(f: dict, allow: dict) -> list[str]:
    problems = []
    reads = {(r['table'], role, col) for r in allow.get('reads', []) for role in r['roles'] for col in r['columns']}
    table_writes = {(t, role, p) for t, roles in allow.get('tables', {}).items()
                    for role, privs in roles.items() for p in privs}
    seq_privs = {(s, role, p) for s, roles in allow.get('sequences', {}).items()
                 for role, privs in roles.items() for p in privs}
    funcs = {(e['function'], role) for e in allow.get('functions', []) for role in e['roles']}

    for rel, kind, grantee, priv in f['relation_acl']:
        if grantee == 'PUBLIC':
            problems.append(f'PUBLIC holds {priv} on {rel}')
        elif kind == 'S':
            if (rel, grantee, priv) not in seq_privs:
                problems.append(f'{grantee} holds {priv} on sequence {rel}')
        elif (rel, grantee, priv) not in table_writes:
            problems.append(f'{grantee} holds table-level {priv} on {rel} (reads are column grants only)')
    held_reads = set()
    for rel, col, role, priv in f['column_acl']:
        if priv == 'SELECT':
            held_reads.add((rel, role, col))
            if (rel, role, col) not in reads and (rel, role, 'SELECT') not in table_writes:
                problems.append(f'{role} may read {rel}.{col}, which is not on the read allowlist')
        elif (rel, role, priv) not in table_writes:
            problems.append(f'{role} holds {priv} on {rel}.{col}')
    existing = {(rel, col) for rel, col in f['columns']}
    for rel, role, col in sorted(reads - held_reads):
        if (rel, col) not in existing:
            problems.append(f'the read allowlist names {rel}.{col}, which does not exist')
        else:
            problems.append(f'{role} may not read allowlisted {rel}.{col} (a shipped client reads it)')

    held_funcs = {(fn, role) for fn, role in f['function_exec']}
    for fn, role in sorted(held_funcs - funcs):
        problems.append(f'{role} may execute {fn}, which is not on the allowlist')
    for fn, role in sorted(funcs - held_funcs):
        problems.append(f'{role} may not execute allowlisted {fn}' if fn in f['functions']
                        else f'allowlisted function {fn} does not exist')
    for fn in f['public_exec']:
        problems.append(f'PUBLIC holds EXECUTE on {fn}')

    for rel, name, cmd in f['write_policies']:
        problems.append(f'write policy "{name}" ({cmd}) on {rel} applies to a request role')
    for rel, name, role, kind, target, ok in f['policy_needs']:
        if ok != 'true':
            what = 'calls' if kind == 'function' else 'sub-selects'
            problems.append(f'policy "{name}" on {rel} {what} {target}, which {role} may not use')

    allowed_storage = set(allow.get('storage_policies', []))
    for rel, name in f['storage_policies']:
        if name not in allowed_storage:
            problems.append(f'storage policy "{name}" on {rel} applies to a request role')
    for bucket in f['public_buckets']:
        if bucket not in set(allow.get('public_buckets', [])):
            problems.append(f'storage bucket "{bucket}" is public')

    for owner, nsp, objtype, grantee, priv in f['default_acl']:
        if objtype in ('r', 'S', 'f') and grantee in ('PUBLIC',) + REQUEST_ROLES:
            problems.append(f'default privilege of {owner} ({nsp or "global"}, {objtype}) grants {priv} to {grantee}')
    for owner in f['default_roles']:
        if owner not in f['global_function_default']:
            problems.append(f'{owner} has no global function default: every function it creates is EXECUTE for PUBLIC')
    for role, can_read, can_insert, can_seq, can_exec in f['probe']:
        for label, held in (('reads', can_read), ('inserts into', can_insert)):
            if held == 'true':
                problems.append(f'{role} {label} a table created after the last migration')
        if can_seq == 'true':
            problems.append(f'{role} uses a sequence created after the last migration')
        if can_exec == 'true':
            problems.append(f'{role} executes a function created after the last migration')
    return problems


# --- the replay ---------------------------------------------------------------

def replay(db: str, teeth_before: str | None, allow: dict) -> int:
    admin_db = os.environ.get('PGDATABASE_ADMIN', 'postgres')
    psql(admin_db, f'DROP DATABASE IF EXISTS "{db}"')
    psql(admin_db, f'CREATE DATABASE "{db}"')
    psql(db, file=STUB)
    files = sorted(MIGRATIONS.glob('*.sql'))
    teeth_done = teeth_before is None
    for path in files:
        if not teeth_done and path.name >= teeth_before:
            problems = judge(facts(db), allow)
            if not problems:
                print(f'TEETH FAILED: the check finds nothing before {path.name}; it would not see the hole it guards')
                return 1
            print(f'teeth: before {path.name} the check reports {len(problems)} problem(s), as it must; e.g.')
            for p in problems[:3]:
                print(f'  - {p}')
            teeth_done = True
        psql(db, file=path)
        print(f'applied {path.name}')
    if not teeth_done:
        print(f'TEETH FAILED: no migration sorts at or after {teeth_before}')
        return 1
    problems = judge(facts(db), allow)
    if problems:
        print(f'\n{len(problems)} request-role privilege problem(s) after {len(files)} migrations:')
        for p in problems:
            print(f'  - {p}')
        print(f'\nFix the migration, or (a decision) add the entry with its reason to {ALLOWLIST.relative_to(REPO)} '
              'and grant it by name in a migration.')
        return 1
    print(f'\nOK: {len(files)} migrations; the request roles hold exactly the allowlist '
          f'({sum(len(r["columns"]) for r in allow["reads"])} columns readable, '
          f'{sum(len(e["roles"]) for e in allow["functions"])} EXECUTE grants, no write).')
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--db', default='edubotics_replay')
    ap.add_argument('--teeth-before', default=None,
                    help='file-name prefix: the check must FAIL right before the first migration at or after it')
    args = ap.parse_args(argv)
    allow = json.loads(ALLOWLIST.read_text(encoding='utf-8'))
    return replay(args.db, args.teeth_before, allow)


if __name__ == '__main__':
    sys.exit(main())
