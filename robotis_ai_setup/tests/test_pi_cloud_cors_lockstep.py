# Copyright 2026 Sven Danil Borodun (EduBotics). All rights reserved.
#
# Proprietary and confidential. Licensed under the EduBotics End-User
# Licence Agreement; see LICENSE at the repository root.
"""The cloud routes any browser origin may call are EXACTLY the calls the
student SPA makes (2026-11-14).

A Pi serves the student SPA at a LAN origin ``ALLOWED_ORIGINS`` cannot list, so
every bearer call that SPA makes to the cloud API must be on
``cloud_training_api/app/main.py::_ANY_ORIGIN_BEARER_ROUTES`` (else it fails its
CORS preflight on a Pi), and nothing else may be (the list widens the API's
CORS surface). This test derives the student build's calls from the SOURCE and
compares both ways:

* the student build is every module reachable from ``src/index.js`` without the
  teacher web's root ``src/WebApp.js`` (``App.js`` renders ``WebApp`` only when
  ``REACT_APP_MODE=web``), following static imports, re-exports and dynamic
  ``import()``;
* a service function counts when a reachable module imports it by name, or
  imports its module as a namespace and calls ``.<name>(`` (``assetCommands``
  receives ``workflowApi`` as an injected ``api``);
* its call is read off its body: ``apiRequest(<path>, '<METHOD>', …)`` or
  ``fetch(`${CLOUD_API_URL}<path>`, {method: …})``, every ``${…}`` that is not a
  module constant a path parameter, the query string dropped;
* a cloud call outside ``services/`` lives in a file that imports
  ``CLOUD_API_URL``; each such file is named below with its calls, so a new one
  fails here until it is classified.

``/version`` is public (``_PUBLIC_READ_PATHS``, its own star) and stays off the
bearer list. Neither the student build nor the list may name a ``/teacher`` or
``/admin`` route: those keep the strict ``ALLOWED_ORIGINS`` rule. Also pinned:
no cloud call asks for credentials (``credentials: 'include'``), so the star
without ``Access-Control-Allow-Credentials`` serves every call."""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "physical_ai_tools" / "physical_ai_manager" / "src"
SERVICES = SRC / "services"
MAIN = REPO / "robotis_ai_setup" / "cloud_training_api" / "app" / "main.py"
ROOT = SRC / "index.js"
WEB_ONLY_ROOT = SRC / "WebApp.js"

# Cloud calls outside services/ (the files that import CLOUD_API_URL), each with
# the text that proves it in its file.
NON_SERVICE_CLOUD_CALLS = {
    "features/piAuth/piAuth.js": ({("POST", "/pi/tickets")}, ("TICKET_PATH = '/pi/tickets'", "method: 'POST'")),
    "features/legal/legalLinks.js": ({("GET", "/version")}, ("/version`", "method: 'GET'")),
}

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
_STATIC_IMPORT = re.compile(r"^\s*import\s+([^;'\"]*?)\s+from\s+['\"]([^'\"]+)['\"]", re.M)
_BARE_IMPORT = re.compile(r"^\s*import\s+['\"]([^'\"]+)['\"]", re.M)
_REEXPORT = re.compile(r"^\s*export\s+[^;'\"]*?\s+from\s+['\"]([^'\"]+)['\"]", re.M)
_DYNAMIC_IMPORT = re.compile(r"\bimport\(\s*['\"]([^'\"]+)['\"]\s*\)")
_EXPORT_HEAD = re.compile(r"^export\s+(?:async\s+)?(?:function\s+(\w+)|const\s+(\w+)\s*=)", re.M)
_MODULE_STRING_CONST = re.compile(r"^const\s+(\w+)\s*=\s*'([^']*)'\s*;", re.M)
_API_REQUEST_HEAD = re.compile(r"\bapiRequest\(\s*")
_CLOUD_FETCH_HEAD = re.compile(r"\bfetch\(\s*(?=`\$\{CLOUD_API_URL\})")
_METHOD_ARG = re.compile(r"\s*,\s*['\"](" + "|".join(METHODS) + r")['\"]")
_FETCH_OPTIONS = re.compile(r"\s*,\s*\{([^}]*)\}")
_IDENT = re.compile(r"[A-Za-z_$][\w$]*")
_CLOUD_BASE = "{CLOUD_API_URL}"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _resolve(importer: Path, spec: str):
    if not spec.startswith("."):
        return None
    base = (importer.parent / spec).resolve()
    for candidate in (base, base.with_name(base.name + ".js"), base.with_name(base.name + ".jsx"),
                      base / "index.js", base / "index.jsx"):
        if candidate.is_file() and candidate.suffix in (".js", ".jsx"):
            return candidate
    return None


def _bindings(text: str):
    """(named, namespaces) of an import binding list: ``X, { a, b as c }``."""
    named = set()
    for group in re.findall(r"\{([^}]*)\}", text):
        for part in group.split(","):
            part = part.strip()
            if part:
                named.add(part.split(" as ")[0].strip())
    namespaces = set(re.findall(r"\*\s+as\s+(\w+)", text))
    return named, namespaces


def student_graph():
    """(reachable files, {service file: named imports}, {service files imported as a namespace})."""
    seen, queue = set(), [ROOT]
    named_by_service: dict[Path, set] = {}
    namespace_services: set[Path] = set()
    while queue:
        path = queue.pop()
        if path in seen or path == WEB_ONLY_ROOT:
            continue
        seen.add(path)
        text = _read(path)
        edges = []
        for binding, spec in _STATIC_IMPORT.findall(text):
            edges.append((spec, binding))
        edges += [(spec, None) for spec in _BARE_IMPORT.findall(text)]
        edges += [(spec, "*") for spec in _REEXPORT.findall(text)]
        edges += [(spec, "*") for spec in _DYNAMIC_IMPORT.findall(text)]
        for spec, binding in edges:
            target = _resolve(path, spec)
            if target is None:
                continue
            if target.parent == SERVICES and binding is not None:
                if binding == "*":
                    namespace_services.add(target)
                else:
                    named, namespaces = _bindings(binding)
                    named_by_service.setdefault(target, set()).update(named)
                    if namespaces:
                        namespace_services.add(target)
            queue.append(target)
    return seen, named_by_service, namespace_services


def _read_literal(text: str, i: int, constants: dict):
    """The JS string or template literal starting at ``text[i]`` as
    ``(value, end)``: every ``${…}`` (nested template literals included) is a
    module string constant when it names one, ``{CLOUD_API_URL}`` for the base,
    else the path parameter ``{}``. ``(None, i)`` when no literal starts there."""
    quote = text[i:i + 1]
    if quote in ("'", '"'):
        j = text.index(quote, i + 1)
        return text[i + 1:j], j + 1
    if quote != "`":
        return None, i
    out, j = [], i + 1
    while text[j] != "`":
        if text[j] == "\\":
            out.append(text[j:j + 2])
            j += 2
        elif text.startswith("${", j):
            depth, k = 1, j + 2
            while depth:
                if text[k] in "'\"`":
                    _, k = _read_literal(text, k, constants)
                    continue
                depth += {"{": 1, "}": -1}.get(text[k], 0)
                k += 1
            expr = text[j + 2:k - 1].strip()
            out.append(_CLOUD_BASE if expr == "CLOUD_API_URL" else constants.get(expr, "{}"))
            j = k
        else:
            out.append(text[j])
            j += 1
    return "".join(out), j + 1


def _route(value: str) -> str:
    """A call's path as a route: the query string, and a placeholder appended
    to a literal segment (``…/credit-history${query}``), dropped."""
    path = value.split("?", 1)[0]
    return re.sub(r"(?<=[^/]){}$", "", path)


def service_calls(path: Path):
    """{exported function: {(METHOD, path)}} of one services/ module."""
    text = _read(path)
    constants = dict(_MODULE_STRING_CONST.findall(text))
    heads = list(_EXPORT_HEAD.finditer(text))
    out = {}
    for i, head in enumerate(heads):
        name = head.group(1) or head.group(2)
        body = text[head.start():heads[i + 1].start() if i + 1 < len(heads) else len(text)]
        calls = set()
        for m in _API_REQUEST_HEAD.finditer(body):
            value, end = _read_literal(body, m.end(), constants)
            if value is None:
                ident = _IDENT.match(body, m.end())
                if not ident or ident.group() not in constants:
                    continue  # a generic helper's own `endpoint` parameter
                value, end = constants[ident.group()], ident.end()
            method = _METHOD_ARG.match(body, end)
            if method:
                calls.add((method.group(1), _route(value)))
        for m in _CLOUD_FETCH_HEAD.finditer(body):
            value, end = _read_literal(body, m.end(), constants)
            route = _route(value[len(_CLOUD_BASE):])
            if not route.startswith("/"):
                continue  # the generic `${CLOUD_API_URL}${endpoint}` of a local apiRequest
            options = _FETCH_OPTIONS.match(body, end)
            verb = re.search(r"method:\s*['\"](\w+)['\"]", options.group(1) if options else "")
            calls.add((verb.group(1) if verb else "GET", route))
        out[name] = calls
    return out


def student_calls():
    reachable, named_by_service, namespace_services = student_graph()
    texts = {p: _read(p) for p in reachable}
    member_calls = set()
    for text in texts.values():
        member_calls.update(re.findall(r"\.(\w+)\(", text))
    calls = set()
    for service in sorted(SERVICES.glob("*.js")):
        if service not in reachable:
            continue
        for fn, fn_calls in service_calls(service).items():
            if fn in named_by_service.get(service, set()) or (
                    service in namespace_services and fn in member_calls):
                calls |= fn_calls
    for rel, (fn_calls, _evidence) in NON_SERVICE_CLOUD_CALLS.items():
        if SRC / rel in reachable:
            calls |= fn_calls
    return calls


def main_literal(name):
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        targets = node.targets if isinstance(node, ast.Assign) else []
        if any(getattr(t, "id", None) == name for t in targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in {MAIN}")


def listed_routes():
    return {(m, re.sub(r"\{[^}]+\}", "{}", t)) for m, t in main_literal("_ANY_ORIGIN_BEARER_ROUTES")}


def product_sources():
    for path in sorted(SRC.rglob("*")):
        if path.suffix in (".js", ".jsx") and "__tests__" not in path.parts:
            yield path


class StudentCloudCallsAreTheAnyOriginList(unittest.TestCase):
    def test_the_student_build_makes_exactly_the_listed_calls_plus_public_version(self):
        calls = student_calls()
        listed = listed_routes()
        missing = sorted(calls - listed - {("GET", "/version")})
        extra = sorted(listed - calls)
        self.assertEqual(missing, [], "a student cloud call a Pi could not make: add it to "
                                      "main.py::_ANY_ORIGIN_BEARER_ROUTES")
        self.assertEqual(extra, [], "an any-origin route the student build never calls: remove it")
        self.assertIn(("GET", "/version"), calls)

    def test_the_known_shape_of_the_calls(self):
        calls = student_calls()
        self.assertGreaterEqual(len(calls), 35)
        for call in (("GET", "/me"), ("POST", "/me/hf-token/reveal"), ("POST", "/pi/tickets"),
                     ("PATCH", "/workflows/{}/trajectories/{}"), ("POST", "/inference/sessions/{}/end")):
            self.assertIn(call, calls)
        # declared by the services, but called by no module of the student build
        for unused in (("PATCH", "/me"), ("GET", "/trainings/{}"), ("DELETE", "/workflows/{}"),
                       ("PATCH", "/datasets/{}"), ("DELETE", "/datasets/{}")):
            self.assertNotIn(unused, calls)

    def test_version_is_public_and_not_a_bearer_route(self):
        self.assertEqual(main_literal("_PUBLIC_READ_PATHS"), ("/version",))
        self.assertNotIn(("GET", "/version"), listed_routes())

    def test_no_teacher_or_admin_route_on_either_side(self):
        for method, route in sorted(student_calls() | listed_routes()):
            with self.subTest(method=method, route=route):
                self.assertFalse(route.startswith(("/teacher", "/admin", "/trainings/worker")))

    def test_the_teacher_web_is_not_part_of_the_student_build(self):
        reachable, _, _ = student_graph()
        self.assertNotIn(WEB_ONLY_ROOT, reachable)
        self.assertIn(SRC / "StudentApp.js", reachable)
        self.assertIn("APP_MODE === 'web' ? (\n      <WebApp />", _read(SRC / "App.js"))
        self.assertFalse(any((SERVICES / name) in reachable
                             for name in ("teacherApi.js", "adminApi.js", "workgroupsApi.js")))


class EveryCloudCallerIsKnown(unittest.TestCase):
    def test_only_cloudconfig_reads_the_build_variable(self):
        readers = [p for p in product_sources() if "process.env.REACT_APP_CLOUD_API_URL" in _read(p)]
        self.assertEqual(readers, [SERVICES / "cloudConfig.js"])

    def test_every_file_outside_services_that_builds_a_cloud_url_is_classified(self):
        callers = sorted(
            str(p.relative_to(SRC)) for p in product_sources()
            if p.parent != SERVICES
            and re.search(r"import\s*\{[^}]*\bCLOUD_API_URL\b[^}]*\}\s*from", _read(p)))
        self.assertEqual(callers, sorted(NON_SERVICE_CLOUD_CALLS))
        for rel, (_calls, evidence) in NON_SERVICE_CLOUD_CALLS.items():
            text = _read(SRC / rel)
            for needle in evidence:
                self.assertIn(needle, text, f"{rel}: {needle}")

    def test_every_service_call_was_read(self):
        # A services/ module that reaches the cloud through a shape this parser
        # does not know would silently drop out; every exported function that
        # mentions apiRequest or CLOUD_API_URL must yield a call.
        for service in sorted(SERVICES.glob("*.js")):
            if service.name in ("apiClient.js", "cloudConfig.js"):
                continue
            text = _read(service)
            for fn, calls in service_calls(service).items():
                body_start = re.search(rf"^export\s+(?:async\s+)?(?:function\s+{fn}\b|const\s+{fn}\b)", text, re.M)
                body = text[body_start.start():]
                nxt = _EXPORT_HEAD.search(body, 1)
                body = body[:nxt.start()] if nxt else body
                uses = re.search(r"\bapiRequest\(\s*['\"`\w]", body) or "${CLOUD_API_URL}/" in body
                with self.subTest(service=service.name, fn=fn):
                    self.assertEqual(bool(calls), bool(uses))

    def test_no_cloud_call_asks_for_credentials(self):
        for path in product_sources():
            text = _read(path)
            with self.subTest(path=str(path.relative_to(SRC))):
                self.assertNotRegex(text, r"credentials\s*:\s*['\"]include['\"]")
                self.assertNotIn("withCredentials", text)


if __name__ == "__main__":
    unittest.main()
