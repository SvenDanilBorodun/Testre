"""The Modal app file, read as an AST (no ``modal`` import here; CI's modal-import-validate imports it
four times under the real SDK). Spec §3.2, D13, D21, D24, Q-N1, C9.

One file, two baked variants (prod / dev) and a snapshot flag; a SESSIONED ``@app.server`` with the
measured settings; nothing of the superseded shared-proxy design anywhere.
"""

from __future__ import annotations

import ast
from pathlib import Path
import re

import pytest

import cloud_protocol as P

PKG = Path(__file__).resolve().parents[1]
APP_SRC = (PKG / 'inference_app.py').read_text(encoding='utf-8')
TREE = ast.parse(APP_SRC)


def _assign(name):
    for node in TREE.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return node.value
    raise AssertionError(f'no module-level {name} =')


def _class(name):
    return next(n for n in TREE.body if isinstance(n, ast.ClassDef) and n.name == name)


def _call_name(node):
    f = node.func if isinstance(node, ast.Call) else node
    parts = []
    while isinstance(f, ast.Attribute):
        parts.append(f.attr)
        f = f.value
    if isinstance(f, ast.Name):
        parts.append(f.id)
    return '.'.join(reversed(parts))


def _kw(call, name):
    for k in call.keywords:
        if k.arg == name:
            return k.value
    raise AssertionError(f'{_call_name(call)} has no {name}=')


def test_variants_table():
    variants = ast.literal_eval(_assign('VARIANTS'))
    assert set(variants) == {'prod', 'dev'}
    assert variants['prod'] == {'app': 'edubotics-inference', 'max_containers': 20,
                                'secret': 'edubotics-inference-secrets',
                                'required': ['SUPABASE_URL', 'SUPABASE_ANON_KEY', 'HF_TOKEN']}
    assert variants['dev'] == {'app': 'edubotics-inference-dev', 'max_containers': 2,
                               'secret': 'edubotics-inference-dev-secrets',
                               'required': ['HF_TOKEN', 'DEV_SESSION_TOKEN', 'DEV_MODEL_REPO', 'DEV_MODEL_REVISION']}
    assert variants['prod']['app'] != variants['dev']['app']           # a dev deploy never overwrites prod
    assert ast.literal_eval(_assign('LEROBOT_VERSION')) == '0.5.1'
    assert ast.literal_eval(_assign('SERVER_PORT')) == 8000
    assert 'os.environ.get("EDUBOTICS_INFERENCE_VARIANT", "prod")' in APP_SRC
    assert 'os.environ.get("EDUBOTICS_INFERENCE_SNAPSHOT", "1") == "1"' in APP_SRC
    assert 'raise RuntimeError' in APP_SRC                              # an unknown variant refuses to import


def test_the_class_is_a_sessioned_server_with_the_measured_settings():
    cls = _class('CloudInference')
    decorators = [_call_name(d) for d in cls.decorator_list]
    assert decorators == ['app.server', 'modal.sessioned']             # app.server(...) on top of sessioned()
    server = cls.decorator_list[0]
    consts = {k.arg: ast.literal_eval(k.value) for k in server.keywords
              if isinstance(k.value, ast.Constant)}
    assert consts == {'gpu': 'T4', 'routing_region': 'eu-west', 'compute_region': 'eu', 'max_concurrency': 6,
                      'target_concurrency': 4, 'min_containers': 0, 'scaledown_window': 300,
                      'startup_timeout': 300, 'cpu': 2.0}
    assert ast.unparse(_kw(server, 'port')) == 'SERVER_PORT'
    assert ast.unparse(_kw(server, 'enable_memory_snapshot')) == 'SNAPSHOT'
    assert ast.unparse(_kw(server, 'max_containers')) == "CFG['max_containers']"
    assert ast.unparse(_kw(server, 'secrets')) == '[SECRET]'
    assert ast.unparse(_kw(server, 'image')) == 'image'
    assert {k.arg for k in server.keywords} == {'image', 'gpu', 'port', 'routing_region', 'compute_region',
                                                'max_concurrency', 'target_concurrency', 'min_containers',
                                                'max_containers', 'scaledown_window', 'startup_timeout',
                                                'enable_memory_snapshot', 'cpu', 'secrets'}
    methods = {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}
    assert '__init__' not in methods
    assert [ast.unparse(d) for d in methods['load_libraries'].decorator_list] == ['modal.enter(snap=SNAPSHOT)']
    assert [ast.unparse(d) for d in methods['serve'].decorator_list] == ['modal.enter(snap=False)']
    assert [ast.unparse(d) for d in methods['stop'].decorator_list] == ['modal.exit()']
    serve = ast.unparse(methods['serve'])
    assert 'StaticSessionBackend.from_env()' in serve and 'SupabaseSessionBackend.from_env()' in serve
    assert 'if VARIANT == "dev"' in serve.replace("'", '"')
    assert 'access_log=False' in serve and 'port=SERVER_PORT' in serve


def test_nothing_of_the_superseded_designs_anywhere():
    names = set()
    for node in ast.walk(TREE):
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        if isinstance(node, ast.keyword) and node.arg:
            names.add(node.arg)
            if node.arg == 'unauthenticated':
                assert not (isinstance(node.value, ast.Constant) and node.value.value is True)
    for forbidden in ('requires_proxy_auth', 'asgi_app', 'concurrent', 'method', 'ws_max_size', 'web_server',
                      'fastapi_endpoint', 'unauthenticated'):
        assert forbidden not in names, forbidden
    for text in ('requires_proxy_auth', 'Modal-Key', 'Modal-Secret', 'MODAL_INFERENCE_PROXY', 'proxy_key',
                 'proxy_secret', 'websocket', '/v1/ws'):
        assert text.lower() not in APP_SRC.lower(), text
    assert not (PKG / 'inference_app_dev.py').exists()


def test_the_baked_env_and_the_image():
    env = next(n for n in ast.walk(TREE) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and n.func.attr == 'env')
    keys = {ast.literal_eval(k) for k in env.args[0].keys}
    assert keys == {'PYTHONUNBUFFERED', 'HF_HUB_DISABLE_TELEMETRY', 'OPENCV_IO_MAX_IMAGE_PIXELS',
                    'EDUBOTICS_INFERENCE_SHA', 'EDUBOTICS_INFERENCE_VARIANT', 'EDUBOTICS_INFERENCE_SNAPSHOT'}
    assert '"OPENCV_IO_MAX_IMAGE_PIXELS": "16777216"' in APP_SRC                       # D25 belt
    files = re.findall(r'add_local_file\(([^,]+), "([^"]+)"\)', APP_SRC)
    assert sorted(dst for _, dst in files) == ['/root/chunk_engine.py', '/root/cloud_protocol.py',
                                                '/root/server_core.py', '/root/session_backend.py']
    assert "torchvision.models.resnet18(weights='IMAGENET1K_V1')" in APP_SRC              # D11 baked
    assert 'python -m pip uninstall -y torchcodec || true' in APP_SRC
    assert 'modal.Image.debian_slim(python_version="3.12")' in APP_SRC
    # Every mounted file exists in the checkout (add_local_file is lazy: a typo would only show at deploy).
    shared = PKG.parents[1] / 'physical_ai_tools' / 'physical_ai_server' / 'physical_ai_server' / 'inference'
    for src, dst in files:
        base, _, name = src.partition(' / ')
        root = {'SHARED': shared, 'HERE': PKG}[base.strip()]
        assert (root / ast.literal_eval(name)).is_file(), src


def test_the_guarded_parents_index():
    for node in ast.walk(TREE):
        if isinstance(node, ast.Subscript) and ast.unparse(node).endswith('.parents[1]'):
            parent = next(p for p in ast.walk(TREE) if isinstance(p, ast.IfExp) and p.body is node)
            assert ast.unparse(parent.test) == 'len(HERE.parents) > 1'
    assert 'HERE.parents[1] if len(HERE.parents) > 1 else Path("/")' in APP_SRC


def test_cloud_base_url_is_the_prod_servers_host():
    variants = ast.literal_eval(_assign('VARIANTS'))
    region = ast.literal_eval(_kw(_class('CloudInference').decorator_list[0], 'routing_region'))
    host = f"svendanilborodun--{variants['prod']['app']}-{'CloudInference'.lower()}.{region}.modal.direct"
    assert P.CLOUD_BASE_URL == f'https://{host}'


def test_the_smoke_test_function():
    fn = next(n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == 'smoke_test')
    (dec,) = fn.decorator_list
    assert _call_name(dec) == 'app.function'
    assert {k.arg: ast.unparse(k.value) for k in dec.keywords} == {
        'image': 'image', 'gpu': "'T4'", 'region': "'eu'", 'secrets': '[SECRET]', 'timeout': '600'}
    body = ast.unparse(fn)
    for needle in ('pretrained_backbone_weights=None', 'ChunkEngine.load', 'missing_secrets', 'chunk_shape',
                   'gpu_ms', 'torch.cuda.is_available()'):
        assert needle in body, needle


@pytest.mark.parametrize('name', ['server_core.py', 'session_backend.py'])
def test_container_import_paths(name):
    """In the container these files are /root/<name>; Path('/root').parents has ONE entry."""
    src = (PKG / name).read_text(encoding='utf-8')
    namespace = {'__file__': f'/root/{name}', '__name__': f'container_{name[:-3]}'}
    exec(compile(src, f'/root/{name}', 'exec', dont_inherit=True), namespace)   # noqa: S102 - the import is the test
    assert namespace['_ROOT'] is None and namespace['_SHARED'] is None
