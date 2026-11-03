"""OMX camera station: the cross-package contract in lockstep (plan v6.13 §11).

The station spans three work packages and four places:

  * GUI (WP-A):       ``gui/app/camera_bridge.py`` (STATION_SCENE_CAM_ID = 200),
                      ``gui/app/constants.py`` (STATION_OPTION_OFFERED);
  * arm (WP-A):       ``docker/open_manipulator/camera_ingest_node.py`` (cam_id 200 →
                      ``/station_scene/image_raw/compressed``, frame_id ``station_scene``);
  * server (WP-B):    ``station/texts_de.py`` (the codes, the topic), the three ``.srv``
                      files, ``station/layout_data.py`` (the one layout source);
  * page + mat (WP-C): ``physical_ai_manager/src/**`` (the preview topic, the reason /
                      end-reason / result codes in its copy module) and
                      ``tools/generate_omx_station_mat.py`` (reads ``layout_data.py``).

Compared from the shipped sources as TEXT / AST (no rclpy, no OpenCV): cam_id 200;
the topic and frame id; the reason, end-reason and result codes in the ``.srv``
comments, the server and the React copy; the layout hash in ``layout_data.py`` and
the generator; ``STATION_OPTION_OFFERED`` equal to "a released layout ships".
Every file is REQUIRED: a missing one fails, it never skips.
"""

import ast
import importlib.util
import pathlib
import re
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
PAS = REPO / 'physical_ai_tools' / 'physical_ai_server' / 'physical_ai_server'
STATION = PAS / 'station'
SRV = REPO / 'physical_ai_tools' / 'physical_ai_interfaces' / 'srv'
CAMERA_BRIDGE = REPO / 'robotis_ai_setup' / 'gui' / 'app' / 'camera_bridge.py'
GUI_CONSTANTS = REPO / 'robotis_ai_setup' / 'gui' / 'app' / 'constants.py'
INGEST = REPO / 'robotis_ai_setup' / 'docker' / 'open_manipulator' / 'camera_ingest_node.py'
REACT_SRC = REPO / 'physical_ai_tools' / 'physical_ai_manager' / 'src'
GENERATOR = REPO / 'tools' / 'generate_omx_station_mat.py'
COMMUNICATOR = PAS / 'communication' / 'communicator.py'

TOPIC = '/station_scene/image_raw/compressed'
FRAME_ID = 'station_scene'


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _texts():
    """station/texts_de.py is stdlib-only and imports nothing of the package."""
    return _load(STATION / 'texts_de.py', 'station_texts_de_lockstep')


def _require(path):
    if not path.is_file():
        raise AssertionError(f'required file missing: {path.relative_to(REPO)}')
    return path.read_text(encoding='utf-8')


def _srv_codes(srv_text, field):
    """The `A | B | C` list in the comment of ``string <field>`` of a .srv
    response, parenthesised notes and quotes dropped."""
    for line in srv_text.splitlines():
        m = re.match(rf'\s*string\s+{field}\s*#(.*)$', line)
        if m:
            comment = re.sub(r'\([^)]*\)', '', m.group(1))
            comment = comment.split(':', 1)[-1] if field == 'end_reason' else comment
            return {c.strip() for c in comment.split('|')
                    if re.fullmatch(r'[A-Z_]+', c.strip())}
    raise AssertionError(f'field {field} not found')


def _react_files():
    if not REACT_SRC.is_dir():
        raise AssertionError(f'required directory missing: {REACT_SRC.relative_to(REPO)}')
    return [p for p in REACT_SRC.rglob('*') if p.suffix in ('.js', '.jsx')
            and '__tests__' not in p.parts and '.test.' not in p.name]


def _int_constant(text, name):
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name and isinstance(node.value, ast.Constant):
                    return node.value.value
    return None


class CamIdAndTopic(unittest.TestCase):
    def test_cam_id_200_in_the_gui_and_the_ingest_node(self):
        bridge = _require(CAMERA_BRIDGE)
        self.assertEqual(_int_constant(bridge, 'STATION_SCENE_CAM_ID'), 200)
        ingest = _require(INGEST)
        self.assertRegex(ingest, r'\b200\b')
        self.assertIn(TOPIC, ingest)
        self.assertIn(FRAME_ID, ingest)

    def test_topic_and_frame_id_in_the_server(self):
        t = _texts()
        self.assertEqual((t.STATION_TOPIC, t.STATION_CAMERA), (TOPIC, FRAME_ID))
        comm = _require(COMMUNICATOR)
        self.assertIn(f"STATION_TOPIC = '{TOPIC}'", comm)
        self.assertIn(f"STATION_CAMERA = '{FRAME_ID}'", comm)

    def test_topic_in_the_react_preview(self):
        hits = [p for p in _react_files() if TOPIC in p.read_text(encoding='utf-8')]
        self.assertTrue(hits, f'{TOPIC} appears in no React source file')


class Codes(unittest.TestCase):
    def setUp(self):
        self.t = _texts()
        self.session = _require(SRV / 'StationSession.srv')
        self.check = _require(SRV / 'StationCheck.srv')
        self.commission = _require(SRV / 'StationCommission.srv')

    def test_srv_comments_equal_the_server_tuples(self):
        t = self.t
        self.assertEqual(_srv_codes(self.session, 'reason_code'), set(t.SESSION_REASON_CODES))
        self.assertEqual(_srv_codes(self.session, 'end_reason'), set(t.END_REASONS))
        self.assertEqual(_srv_codes(self.session, 'state'), set(t.STATES))
        self.assertEqual(_srv_codes(self.check, 'result'), set(t.CHECK_RESULTS))
        self.assertEqual(_srv_codes(self.commission, 'result'), set(t.COMMISSION_RESULTS))
        self.assertIn('uint32 station_protocol', self.session)
        self.assertEqual(t.STATION_PROTOCOL, 6)

    def test_every_code_appears_in_the_react_copy(self):
        t = self.t
        text = '\n'.join(p.read_text(encoding='utf-8') for p in _react_files())
        codes = (set(t.SESSION_REASON_CODES) | set(t.END_REASONS)
                 | set(t.CHECK_RESULTS) | set(t.COMMISSION_RESULTS))
        missing = sorted(c for c in codes if not re.search(rf"['\"`]{c}['\"`]", text))
        self.assertEqual(missing, [], 'codes the React station copy does not know')

    def test_every_commission_metric_reaches_the_wizard(self):
        """Every ``float64`` of the commission answer (the lens, board, world and
        registration results, the lens fx / fy bench gate B7 reads) is merged
        into the wizard's view, so a result line never misses a number the
        server sends."""
        response = self.commission.split('---', 1)[1]
        floats = set(re.findall(r'^\s*float64\s+(\w+)', response, re.M))
        self.assertIn('lens_fx_px', floats)
        self.assertIn('lens_fy_px', floats)
        wizard = _require(REACT_SRC / 'components' / 'Station' / 'StationCommissionWizard.jsx')
        m = re.search(r'const NUMBER_FIELDS = \[(.*?)\];', wizard, re.S)
        self.assertIsNotNone(m, 'NUMBER_FIELDS not found in the wizard')
        fields = set(re.findall(r"'(\w+)'", m.group(1)))
        self.assertEqual(sorted(floats - fields), [],
                         'commission metrics the wizard never shows')

    def test_every_server_code_is_one_the_server_answers(self):
        service = _require(STATION / 'service.py') + _require(STATION / 'state.py')
        for code in self.t.SESSION_REASON_CODES:
            self.assertRegex(service, rf"['\"]{code}['\"]", code)


class Layout(unittest.TestCase):
    def setUp(self):
        self.ld = _load(STATION / 'layout_data.py', 'station_layout_data_lockstep')

    def test_the_generator_reads_layout_data_and_its_hash(self):
        gen = _require(GENERATOR)
        self.assertIn('layout_data.py', gen)
        self.assertIn('layout_sha256', gen, 'the generator prints the module hash')
        self.assertNotRegex(gen, r'def\s+layout_sha256\s*\(',
                            'the generator must call layout_data.layout_sha256, not a copy')
        self.assertIn('board_check_squares', gen,
                      'B1/B2 come from layout_data.board_check_squares')

    def test_station_option_offered_equals_a_released_layout(self):
        released = any(v.get('layout_state') == 'released' for v in self.ld.LAYOUTS.values())
        consts = _require(GUI_CONSTANTS)
        value = None
        for node in ast.walk(ast.parse(consts)):
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == 'STATION_OPTION_OFFERED'
                    for t in node.targets):
                value = node.value.value if isinstance(node.value, ast.Constant) else None
        self.assertIsInstance(value, bool, 'STATION_OPTION_OFFERED must be a bool literal')
        self.assertEqual(value, released)

    def test_the_shipped_layout_is_draft_and_hashes_canonically(self):
        lay = self.ld.LAYOUTS['OMX-STATION-A']
        self.assertEqual(lay['layout_state'], 'draft')
        self.assertEqual(len(self.ld.layout_sha256(lay)), 64)
        self.assertEqual(self.ld.canonical_json(lay),
                         self.ld.canonical_json(dict(reversed(list(lay.items())))))

    def test_reference_ids_are_in_the_station_range_and_disjoint_from_the_catalog(self):
        for lay in self.ld.LAYOUTS.values():
            ids = [r['id'] for r in lay['refs']]
            self.assertTrue(all(100 <= i <= 199 for i in ids))
            self.assertFalse({20, 21} & set(ids))


if __name__ == '__main__':
    unittest.main()
