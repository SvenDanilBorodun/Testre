"""The arm image's Dockerfile names only files that exist, and lists every
plain-COPY script in BOTH its CRLF-strip and its exec-bit line.

No PR job builds the ``open_manipulator`` image, so a renamed or deleted
source file (the driver and its geometry module were renamed together) would
only surface at the next image build, and a script missing from the
``sed -i``/``chmod +x`` lists would ship with CRLF line endings or without its
exec bit. Deps-free: the Dockerfile is read as text.
"""

import os
import re
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ARM_DIR = os.path.join(_HERE, '..', 'docker', 'open_manipulator')
_DOCKERFILE = os.path.join(_ARM_DIR, 'Dockerfile')


def _logical_lines():
    """The Dockerfile with backslash continuations joined."""
    with open(_DOCKERFILE, encoding='utf-8') as handle:
        text = handle.read()
    return re.sub(r'\\\n', ' ', text).splitlines()


def _copies():
    """[(source, destination)] for every single-source ``COPY`` instruction."""
    out = []
    for line in _logical_lines():
        parts = line.split()
        if not parts or parts[0] != 'COPY':
            continue
        args = [p for p in parts[1:] if not p.startswith('--')]
        if len(args) != 2:
            raise AssertionError(f'unexpected COPY form: {line!r}')
        out.append((args[0], args[1]))
    return out


def _bin_scripts(marker):
    """The ``/usr/local/bin/*.py`` paths on the logical line containing
    ``marker`` (the RUN joins the sed and chmod halves with ``&&``)."""
    for line in _logical_lines():
        if marker in line:
            segment = line.split(marker, 1)[1].split('&&', 1)[0]
            return set(re.findall(r'/usr/local/bin/[A-Za-z0-9_]+\.py', segment))
    raise AssertionError(f'no line containing {marker!r}')


class TestArmDockerfileCopySources(unittest.TestCase):

    def test_every_copy_source_exists_in_the_build_context(self):
        copies = _copies()
        self.assertTrue(copies)
        for source, _dest in copies:
            with self.subTest(source):
                self.assertTrue(
                    os.path.isfile(os.path.join(_ARM_DIR, source)),
                    f'COPY {source}: not in docker/open_manipulator/')

    def test_the_sed_and_chmod_lists_equal_the_copied_bin_scripts(self):
        copied = {dest for _src, dest in _copies()
                  if dest.startswith('/usr/local/bin/') and dest.endswith('.py')}
        self.assertTrue(copied)
        self.assertEqual(_bin_scripts("sed -i 's/\\r$//'"), copied)
        self.assertEqual(_bin_scripts('chmod +x'), copied)

    def test_the_feetech_driver_trio_is_copied(self):
        copied = {dest for _src, dest in _copies()}
        for name in ('feetech_arm_node.py', 'feetech_geometry.py',
                     'feetech_bus.py', 'identify_arm.py', 'activation_agent.py'):
            self.assertIn(f'/usr/local/bin/{name}', copied, name)


if __name__ == '__main__':
    unittest.main()
