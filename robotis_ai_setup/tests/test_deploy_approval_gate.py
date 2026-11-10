# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.

"""SA-13 (2026-11-13): every job that changes production waits for a person.

The six jobs that write production (the migration apply and rollback, the two
Railway deploys, the trainer-image variable and W6's update gate) name
``environment: production``; the environment's required reviewers are the
gate (owner setting, docs/deploy/DEPLOY.md section 0). Read as text, stdlib
only: each job block must carry exactly that line, and no other job carries
an environment, so the gated set cannot drift silently either way.
"""

import pathlib
import re
import unittest

_WF = pathlib.Path(__file__).resolve().parents[2] / '.github' / 'workflows'

GATED = {
    ('supabase-migrate.yml', 'apply-production'),
    ('supabase-migrate.yml', 'rollback'),
    ('railway-deploy-cloud-api.yml', 'deploy'),
    ('railway-deploy-teacher-web.yml', 'deploy'),
    ('release.yml', 'trainer-image-var'),
    ('release.yml', 'publish-gui-version'),
}

_JOB_RE = re.compile(r'^  ([A-Za-z0-9_-]+):\n', re.M)


def _jobs(text: str) -> dict:
    body = text[text.index('\njobs:\n') + len('\njobs:\n'):]
    starts = [(m.group(1), m.start()) for m in _JOB_RE.finditer(body)]
    out = {}
    for i, (name, start) in enumerate(starts):
        end = starts[i + 1][1] if i + 1 < len(starts) else len(body)
        out[name] = body[start:end]
    return out


class DeployApprovalGate(unittest.TestCase):
    def test_exactly_the_production_writers_name_the_environment(self):
        found = set()
        for path in sorted(_WF.glob('*.yml')):
            for job, block in _jobs(path.read_text(encoding='utf-8')).items():
                envs = re.findall(r'^    environment:\s*(.+)$', block, re.M)
                if envs:
                    self.assertEqual(envs, ['production'], (path.name, job))
                    found.add((path.name, job))
        self.assertEqual(found, GATED)

    def test_the_runbook_tells_the_owner_to_add_required_reviewers(self):
        doc = (_WF.parents[1] / 'docs' / 'deploy' / 'DEPLOY.md').read_text(encoding='utf-8')
        self.assertIn('## 0. The production approval gate', doc)
        self.assertIn('Required reviewers', doc)
        self.assertIn('named exactly `production`', doc)


if __name__ == '__main__':
    unittest.main()
