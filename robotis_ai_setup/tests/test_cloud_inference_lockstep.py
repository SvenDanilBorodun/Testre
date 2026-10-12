"""Cloud-Inferenz: the frozen contracts held in lockstep across the three packages (spec §9.1/§9.4).

Cloud-Inferenz spans five places in four languages and three containers:

  * robot:    ``physical_ai_server/inference/cloud_contract.py`` (C4/C8), ``cloud_protocol.py`` (C1),
              ``cloud_texts_de.py``, ``StartCloudInference.srv``, ``robot_profiles.py`` (C5) and the node;
  * page:     ``features/cloudInference/robotContract.js``, ``robotCalls.js``, ``taskInfoWire.js``,
              ``gating.js``;
  * GUI:      ``gui/app/docker_manager.py`` (the window-close hook: CLOUD_STOP_SERVICE / CLOUD_STOP_MARKER);
  * database: migration 048 (C3);
  * cloud:    ``cloud_training_api/app/services/inference_sessions.py`` / ``inference_edge.py`` and the
              Modal app ``modal_inference/{session_backend,inference_app,server_core}.py`` (C9, D37).

If one side moved alone nothing would fail loudly: a renamed topic or service is a call or a
subscription that silently never meets its other half, a changed regex makes the database refuse a
real Modal session id after the API minted it, a renamed RPC argument is a PGRST202 for every robot,
an end reason the SQL CHECK does not know is a 22023 at the end of every such run. So the names,
the shapes and the vocabularies are held to one place here.

The JavaScript and most Python files are read as TEXT or as an AST (no import of modal, fastapi,
rclpy or cryptography); ``cloud_contract.py`` (stdlib) and ``cloud_protocol.py`` / ``cloud_texts_de.py``
(stdlib + numpy) are loaded by path. Every file is REQUIRED: a missing one fails, it never skips (a
skipped lockstep is no lockstep).
"""

import ast
import importlib.util
import pathlib
import re
import sys
import types
import unittest
from collections import namedtuple

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PAS = REPO_ROOT / "physical_ai_tools" / "physical_ai_server" / "physical_ai_server"
INFERENCE = PAS / "inference"
CONTRACT_PY = INFERENCE / "cloud_contract.py"
PROTOCOL_PY = INFERENCE / "cloud_protocol.py"
TEXTS_PY = INFERENCE / "cloud_texts_de.py"
NODE_PY = PAS / "physical_ai_server.py"
PROFILES_PY = PAS / "robot_profiles.py"
INTERFACES = REPO_ROOT / "physical_ai_tools" / "physical_ai_interfaces"
SRV = INTERFACES / "srv" / "StartCloudInference.srv"
TASK_INFO_MSG = INTERFACES / "msg" / "TaskInfo.msg"
INTERFACES_CMAKE = INTERFACES / "CMakeLists.txt"
SPA = REPO_ROOT / "physical_ai_tools" / "physical_ai_manager" / "src" / "features" / "cloudInference"
CONTRACT_JS = SPA / "robotContract.js"
CALLS_JS = SPA / "robotCalls.js"
WIRE_JS = SPA / "taskInfoWire.js"
GATING_JS = SPA / "gating.js"
GUI_DOCKER_MANAGER = REPO_ROOT / "robotis_ai_setup" / "gui" / "app" / "docker_manager.py"
API = REPO_ROOT / "robotis_ai_setup" / "cloud_training_api" / "app" / "services"
SESSIONS_PY = API / "inference_sessions.py"
EDGE_PY = API / "inference_edge.py"
MODAL_INFERENCE = REPO_ROOT / "robotis_ai_setup" / "modal_inference"
BACKEND_PY = MODAL_INFERENCE / "session_backend.py"
APP_PY = MODAL_INFERENCE / "inference_app.py"
SERVER_CORE_PY = MODAL_INFERENCE / "server_core.py"
MIGRATION = (REPO_ROOT / "robotis_ai_setup" / "supabase" / "migrations"
             / "20261011120000_048_inference_sessions.sql")


def _require(path):
    """The zero-file floor: a missing input is a failure with the path in it."""
    if not path.is_file():
        raise AssertionError(f"{path.relative_to(REPO_ROOT)} is missing: the Cloud-Inferenz lockstep "
                             "has nothing to compare, and a skipped lockstep is no lockstep")
    return path


def _text(path):
    return _require(path).read_text(encoding="utf-8")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, _require(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_texts(protocol):
    """cloud_texts_de imports `physical_ai_server.inference.cloud_protocol`: hand it the module loaded
    by path through throwaway package stubs, so the node package's __init__ (rclpy) never runs."""
    saved = {k: sys.modules.get(k) for k in ("physical_ai_server", "physical_ai_server.inference",
                                              "physical_ai_server.inference.cloud_protocol")}
    pkg = types.ModuleType("physical_ai_server")
    pkg.__path__ = []
    sub = types.ModuleType("physical_ai_server.inference")
    sub.__path__ = []
    sub.cloud_protocol = protocol
    pkg.inference = sub
    sys.modules.update({"physical_ai_server": pkg, "physical_ai_server.inference": sub,
                        "physical_ai_server.inference.cloud_protocol": protocol})
    try:
        return _load("_cloud_lockstep_texts", TEXTS_PY)
    finally:
        for key, value in saved.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value


def _js_string(text, name):
    matches = re.findall(rf"export const {name} = '([^'\\]*)';", text)
    assert len(matches) == 1, (name, matches)
    return matches[0]


def _js_number(text, name):
    matches = re.findall(rf"export const {name} = ([0-9][0-9_]*(?:\.[0-9]+)?);", text)
    assert len(matches) == 1, (name, matches)
    return float(matches[0].replace("_", ""))


def _js_frozen_list(text, name):
    matches = re.findall(rf"export const {name} = Object\.freeze\(\[([^\]]*)\]\);", text)
    assert len(matches) == 1, (name, matches)
    return re.findall(r"'([^']*)'", matches[0])


def _module_assign(path, name):
    """The literal value of a module-level `NAME = <literal>` (or `NAME: T = <literal>`)."""
    tree = ast.parse(_text(path), filename=str(path))
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else (
            [node.target] if isinstance(node, ast.AnnAssign) else [])
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            return node.value
    raise AssertionError(f"{name} not assigned at module level in {path.name}")


def _regex_pattern(path, name):
    """The pattern string of a module-level `NAME = re.compile(r'...')`."""
    value = _module_assign(path, name)
    assert isinstance(value, ast.Call) and value.args, (path.name, name)
    return ast.literal_eval(value.args[0])


def _env_default(path, name):
    """The default of a module-level `NAME = os.environ.get("ENV", "default")`."""
    value = _module_assign(path, name)
    assert isinstance(value, ast.Call) and len(value.args) == 2, (path.name, name)
    return ast.literal_eval(value.args[1])


def _sql_functions():
    sql = "\n".join(line.split("--", 1)[0] for line in _text(MIGRATION).splitlines())
    out = {}
    for m in re.finditer(r"CREATE FUNCTION public\.(\w+)\((.*?)\)\s*RETURNS", sql, re.S):
        out[m.group(1)] = [part.split()[0] for part in m.group(2).split(",") if part.strip()]
    return out, sql


def _srv_request_fields():
    request = _text(SRV).split("---", 1)[0]
    fields = []
    for line in request.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            fields.append(line.split()[1])
    return fields


def _msg_fields(path):
    fields = []
    for line in _text(path).splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            fields.append(line.split()[1])
    return fields


class TheRobotContract(unittest.TestCase):
    """C4/C8: robotContract.js == cloud_contract.py, and the srv the page calls is the srv the node serves."""

    @classmethod
    def setUpClass(cls):
        cls.contract = _load("_cloud_lockstep_contract", CONTRACT_PY)
        cls.js = _text(CONTRACT_JS)

    def test_the_names(self):
        self.assertEqual(_js_string(self.js, "START_SERVICE"), self.contract.START_SERVICE)
        self.assertEqual(_js_string(self.js, "STOP_SERVICE"), self.contract.STOP_SERVICE)
        self.assertEqual(_js_string(self.js, "STATE_TOPIC"), self.contract.STATE_TOPIC)

    def test_the_state_document(self):
        self.assertEqual(int(_js_number(self.js, "STATE_SCHEMA_VERSION")), self.contract.STATE_SCHEMA_VERSION)
        self.assertEqual(tuple(_js_frozen_list(self.js, "STATE_KEYS")), self.contract.STATE_KEYS)
        self.assertEqual(tuple(_js_frozen_list(self.js, "PHASES")), self.contract.PHASES)
        # A state older than this is no state: more than twice the publish period (with room for
        # rosbridge's 500 ms throttle), or a healthy robot reads as unknown on every other tick.
        self.assertGreaterEqual(_js_number(self.js, "STATE_STALE_MS"), 2 * self.contract.PUBLISH_PERIOD_S * 1000 + 500)

    def test_the_service_types(self):
        self.assertEqual(_js_string(self.js, "START_SERVICE_TYPE"),
                         "physical_ai_interfaces/srv/" + SRV.stem)
        self.assertIn(f'"srv/{SRV.name}"', _text(INTERFACES_CMAKE))
        self.assertEqual(_js_string(self.js, "STOP_SERVICE_TYPE"), "std_srvs/srv/Trigger")
        node = _text(NODE_PY)
        self.assertIn("(cloud_contract.START_SERVICE, StartCloudInference, self.start_cloud_inference_callback)", node)
        self.assertIn("(cloud_contract.STOP_SERVICE, Trigger, self.cloud_inference_stop_callback)", node)
        self.assertIn("from std_srvs.srv import Trigger", node)

    def test_the_srv_fields_are_the_pages_request_keys(self):
        fields = _srv_request_fields()
        self.assertEqual(fields, ["task_info", "session_token", "edge_token"])
        self.assertEqual(_js_frozen_list(self.js, "START_REQUEST_KEYS"), fields)
        calls = _text(CALLS_JS)
        m = re.search(r"new ROSLIB\.ServiceRequest\(\{(.*?)\}\)", calls, re.S)
        self.assertIsNotNone(m, "robotCalls.js builds no start request")
        self.assertEqual(re.findall(r"(\w+):", m.group(1)), fields)

    def test_the_task_info_wire_is_task_info_msg(self):
        wire = _text(WIRE_JS)
        m = re.search(r"const base = \{(.*?)\n  \};", wire, re.S)
        self.assertIsNotNone(m, "taskInfoWire.js has no base object")
        keys = re.findall(r"^\s+(\w+):", m.group(1), re.M)
        self.assertEqual(sorted(keys), sorted(_msg_fields(TASK_INFO_MSG)))
        # the four overrides of a cloud start are TaskInfo fields too
        controller = _text(SPA / "controller.js")
        for key in ("task_type", "policy_path", "fps", "record_inference_mode"):
            self.assertIn(f"{key}:", controller)
            self.assertIn(key, keys)

    def test_the_capability_key(self):
        self.assertIn("'cloud_inference': caps.inferable,", _text(PROFILES_PY))
        self.assertIn("capabilities.cloud_inference === true", _text(GATING_JS))


class TheGuiHook(unittest.TestCase):
    def test_the_gui_names_the_robots_stop_service_and_marker(self):
        contract = _load("_cloud_lockstep_contract_gui", CONTRACT_PY)
        text = _text(GUI_DOCKER_MANAGER)
        service = re.findall(r'^CLOUD_STOP_SERVICE = "([^"]+)"', text, re.M)
        marker = re.findall(r'^CLOUD_STOP_MARKER = "([^"]+)"', text, re.M)
        self.assertEqual(service, [contract.STOP_SERVICE])
        self.assertEqual(marker, [contract.ACTIVE_MARKER_PATH])


class TheDatabaseContract(unittest.TestCase):
    """C3: the Modal app calls the four anon RPCs with exactly the 048 argument names; the end
    reasons every side speaks are the ones the SQL CHECKs accept."""

    @classmethod
    def setUpClass(cls):
        cls.functions, cls.sql = _sql_functions()

    def test_session_backend_calls_the_anon_rpcs_with_every_argument(self):
        tree = ast.parse(_text(BACKEND_PY), filename=str(BACKEND_PY))
        calls = {}
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "_rpc"
                    and len(node.args) == 2 and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[1], ast.Dict)):
                calls[node.args[0].value] = [ast.literal_eval(k) for k in node.args[1].keys]
        self.assertEqual(set(calls), {"inference_session_peek", "inference_session_connect",
                                      "inference_session_heartbeat", "inference_session_end"})
        for name, args in calls.items():
            # ALL of them, always (PostgREST resolves on the name set; omitting p_stats is a 404)
            self.assertEqual(args, self.functions[name], name)
        self.assertIn("anon, service_role;", re.search(
            r"GRANT EXECUTE ON FUNCTION public\.inference_session_end\([^)]*\) TO ([^;]+;)", self.sql).group(0))

    def test_end_reason_maps_equal_the_sql_check(self):
        m = re.search(r"end_reason\s+TEXT CHECK \(end_reason IS NULL OR end_reason IN \((.*?)\)\)", self.sql, re.S)
        reasons = set(re.findall(r"'([a-z_]+)'", m.group(1)))
        self.assertEqual(len(reasons), 11)
        self.assertEqual(set(ast.literal_eval(_module_assign(SESSIONS_PY, "END_REASON_DE"))), reasons)
        self.assertEqual(set(ast.literal_eval(_module_assign(SESSIONS_PY, "EXIT_REASON"))), reasons)

    def test_the_reasons_the_server_ends_with_are_the_ones_the_end_rpc_takes(self):
        protocol = _load("_cloud_lockstep_protocol_bye", PROTOCOL_PY)
        m = re.search(r"p_reason NOT IN \(('robot_stop'[^)]*)\)", self.sql)
        allowed = set(re.findall(r"'([a-z_]+)'", m.group(1)))
        self.assertTrue(set(protocol.BYE_REASONS) <= allowed, protocol.BYE_REASONS)
        self.assertIn("'server_error'", _text(SERVER_CORE_PY))
        self.assertIn("server_error", allowed)


class TheCloudContract(unittest.TestCase):
    """C1/C9/D37: the shapes and names the cloud API, the database, the Modal app and the robot share."""

    @classmethod
    def setUpClass(cls):
        cls.protocol = _load("_cloud_lockstep_protocol", PROTOCOL_PY)
        _, cls.sql = _sql_functions()

    def test_the_two_regexes_are_identical_and_the_sql_check_is_the_same_pattern(self):
        self.assertEqual(_regex_pattern(SESSIONS_PY, "EDGE_TOKEN_RE"), self.protocol.EDGE_TOKEN_RE.pattern)
        self.assertEqual(_regex_pattern(SESSIONS_PY, "MODAL_SESSION_ID_RE"), self.protocol.MODAL_SESSION_ID_RE.pattern)
        sql_patterns = set(re.findall(r"'\^(sd-[^']*)\$'", self.sql))
        self.assertEqual(sql_patterns, {self.protocol.MODAL_SESSION_ID_RE.pattern})
        # the connect token: secrets.token_urlsafe(32) on both sides and in the anon RPCs
        self.assertEqual(_regex_pattern(SESSIONS_PY, "CONNECT_TOKEN_RE"), self.protocol.TOKEN_RE.pattern)
        self.assertIn(f"'^{self.protocol.TOKEN_RE.pattern}$'", self.sql)

    def test_the_headers_and_the_prefetch_path(self):
        for name in ("EDGE_AUTH_HEADER", "CONNECT_AUTH_HEADER", "PREFETCH_PATH"):
            self.assertEqual(ast.literal_eval(_module_assign(EDGE_PY, name)), getattr(self.protocol, name), name)

    def test_the_api_starts_sessions_of_the_prod_app_and_class(self):
        variants = ast.literal_eval(_module_assign(APP_PY, "VARIANTS"))
        tree = ast.parse(_text(APP_PY))
        classes = [n.name for n in tree.body if isinstance(n, ast.ClassDef)]
        self.assertEqual(_env_default(EDGE_PY, "APP"), variants["prod"]["app"])
        self.assertIn(_env_default(EDGE_PY, "CLS"), classes)
        self.assertEqual(_env_default(EDGE_PY, "CLS"), "CloudInference")


class TheEndSentences(unittest.TestCase):
    def test_every_non_retry_error_code_has_a_sentence(self):
        protocol = _load("_cloud_lockstep_protocol_texts", PROTOCOL_PY)
        texts = _load_texts(protocol)
        End = namedtuple("End", "kind code reason server_v")
        for code, retry in protocol.ERROR_CODES.items():
            if retry:
                continue
            reason = "expired" if code == "ended" else None
            sentence = texts.end_sentence(End("server", code, reason, [1]))
            self.assertIsInstance(sentence, str, code)
            self.assertTrue(sentence, code)
            if code not in ("ended", "version"):
                self.assertNotEqual(sentence, texts.INTERNAL_DE, code)
        # a deliberate end has no sentence (the page shows none)
        self.assertEqual(texts.end_sentence(End("server", "ended", "user_stop", None)), "")
        self.assertEqual(texts.end_sentence(End("server", "ended", "robot_stop", None)), "")


if __name__ == "__main__":
    unittest.main()
