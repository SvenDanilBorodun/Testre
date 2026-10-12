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
import server_core as S

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
    # V1-5: the dev model is public, so the dev variant needs no HF_TOKEN (and smoke_test reports none missing).
    assert variants['dev'] == {'app': 'edubotics-inference-dev', 'max_containers': 2,
                               'secret': 'edubotics-inference-dev-secrets',
                               'required': ['DEV_SESSION_TOKEN', 'DEV_MODEL_REPO', 'DEV_MODEL_REVISION']}
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
    assert consts == {'gpu': 'T4', 'routing_region': 'eu-west', 'compute_region': 'eu', 'max_concurrency': 5,
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
    # V1-5: it reports only the variant's own required secrets (the dev variant has no HF_TOKEN)
    assert "for k in CFG['required'] if not os.environ.get(k)" in body


@pytest.mark.parametrize('name', ['server_core.py', 'session_backend.py'])
def test_container_import_paths(name):
    """In the container these files are /root/<name>; Path('/root').parents has ONE entry."""
    src = (PKG / name).read_text(encoding='utf-8')
    namespace = {'__file__': f'/root/{name}', '__name__': f'container_{name[:-3]}'}
    exec(compile(src, f'/root/{name}', 'exec', dont_inherit=True), namespace)   # noqa: S102 - the import is the test
    assert namespace['_ROOT'] is None and namespace['_SHARED'] is None


# ---------------------------------------------------------------- fix round (V1-2, V1-3, V1-5, V1-10)

def _image_steps():
    """The image builder chain in build order: [(method name, Call), ...]."""
    node, steps = _assign('image'), []
    while isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        steps.append((node.func.attr, node))
        node = node.func.value
    return steps[::-1]


def _server_kw(name):
    return ast.literal_eval(_kw(_class('CloudInference').decorator_list[0], name))


def test_five_robots_per_t4_and_the_lru_is_max_concurrency_plus_two():
    """V1-2 (owner): the real engine costs 26-28 ms per chunk under load, 6 sessions put the GPU thread at
    ~97 %; 5 per T4. D27: the count-based LRU holds max_concurrency + 2 models."""
    assert (_server_kw('max_concurrency'), _server_kw('target_concurrency')) == (5, 4)
    assert S.LRU_MAX_MODELS == _server_kw('max_concurrency') + 2 == 7
    assert ast.literal_eval(_assign('VARIANTS'))['prod']['max_containers'] == 20
    assert 'max_models=server_core.LRU_MAX_MODELS' in ast.unparse(
        next(n for n in _class('CloudInference').body if isinstance(n, ast.FunctionDef) and n.name == 'serve'))


def test_the_prod_variant_can_never_serve_a_static_backend():
    """Every use of StaticSessionBackend sits behind ``VARIANT == "dev"`` (the backend's from_env refuses
    any other baked variant too, test_session_backend.py)."""
    uses = [n for n in ast.walk(TREE) if isinstance(n, ast.Attribute) and n.attr == 'StaticSessionBackend']
    assert len(uses) == 1
    guarded = False
    for node in ast.walk(TREE):
        if isinstance(node, (ast.IfExp, ast.If)) and ast.unparse(node.test).replace("'", '"') == 'VARIANT == "dev"':
            body = node.body if isinstance(node, ast.IfExp) else ast.Module(body=node.body, type_ignores=[])
            guarded = guarded or any(n is uses[0] for n in ast.walk(body))
    assert guarded
    assert 'DEV_' not in ''.join(ast.literal_eval(_assign('VARIANTS'))['prod']['required'])


def test_the_warm_up_runs_after_the_snapshot_restore_never_inside_it():
    """V1-3: the warm-up chunk needs CUDA, so it may never run in ``@modal.enter(snap=True)``: that hook only
    imports. It runs per model LOAD inside server_core (build_app's default), reached from serve()."""
    methods = {n.name: n for n in _class('CloudInference').body if isinstance(n, ast.FunctionDef)}
    assert all(isinstance(n, (ast.Import, ast.ImportFrom)) for n in methods['load_libraries'].body)
    build = next(n for n in ast.walk(methods['serve']) if isinstance(n, ast.Call)
                 and ast.unparse(n.func) == 'server_core.build_app')
    assert 'warm_up' not in {k.arg for k in build.keywords}             # the default: warm up every load
    import inspect
    assert inspect.signature(S.build_app).parameters['warm_up'].default is True


def test_the_torch_swap_leaves_no_pin_broken():
    """V1-10: the --force-reinstall swap re-resolves torch's own dependencies from the cu126 index; the
    deploy log showed numpy 2.5.2 (lerobot 0.5.1 needs <2.3.0) and fsspec 2026.7.0 (datasets 4.8.5 needs
    <=2026.2.0). The swap stays (it must replace torch 2.10's cu128 nvidia-* set and triton 3.6.0 with
    torch 2.7.0's cu126 set, which --no-deps would keep); the very next step puts both back from PyPI."""
    steps = _image_steps()
    swap = next(i for i, (name, call) in enumerate(steps) if name == 'pip_install'
                and any(isinstance(a, ast.Constant) and a.value == 'torch==2.7.0' for a in call.args))
    first = steps[swap - 1][1]
    assert steps[swap - 1][0] == 'pip_install'
    assert 'datasets==4.8.5' in [a.value for a in first.args if isinstance(a, ast.Constant)]   # fsspec's cap holder
    assert ast.literal_eval(_kw(steps[swap][1], 'extra_options')) == '--force-reinstall'
    name, repin = steps[swap + 1]
    assert name == 'pip_install' and not repin.keywords                 # PyPI, no index_url, no force
    assert [ast.literal_eval(a) for a in repin.args] == ['numpy>=2.0.0,<2.3.0', 'fsspec==2026.2.0']
    for _, call in steps:
        for k in call.keywords:
            if k.arg == 'extra_options':
                assert '--no-deps' not in ast.literal_eval(k.value)
