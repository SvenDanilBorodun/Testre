#!/usr/bin/env python3
"""Build public/v1-urdf/ (v1.urdf + meshes/*.STL) from the V1 SolidWorks export + Base_part.2.step.

Usage (scratch venv: gmsh==4.15.2 numpy==2.4.6):
  python build_v1_urdf_assets.py <CAD export dir> <Base_part.2.step> <out dir> [--voxel-mm 0.5]
Deterministic: same inputs + versions -> same bytes. Every CAD edit is an exact,
assert-once string replacement; nothing is typed in by hand."""
import argparse, os, struct, sys
import numpy as np

TABLE_DZ_M = 0.006            # base_link frame re-origined to the base bottom (table plane)
# STEP (mm, Autodesk Fusion frame) -> exported base_link frame (m), proven by feature
# matching (4x M2.5 + 2x M5/c'bore + the 11 mm joint-1 hole; 93 % of the shared surface
# within 0.05 mm of the exported Base_part-1 body): x_b = -z_s - 10.75, y_b = -x_s, z_b = y_s - 6.0
STEP_R = np.array([[0, 0, -1], [-1, 0, 0], [0, 1, 0]], float)
STEP_T_MM = np.array([-10.75, 0.0, -6.0])
OLD_BASE_PART_BOUNDS_M = np.array([[-0.07075, -0.075, -0.006], [0.04925, 0.075, 0.037]])
KEEP_BOUNDS_M = (  # joint-1 STS3215, top cover (export base frame, m)
    np.array([[-0.010, -0.01225, 0.0044], [0.035, 0.01225, 0.0407]]),
    np.array([[-0.01599, -0.01825, 0.037], [0.03975, 0.01825, 0.046]]),
)

def read_stl(path):
    raw = open(path, 'rb').read()
    n = struct.unpack('<I', raw[80:84])[0]
    body = np.frombuffer(raw[84:84 + 50 * n], dtype=np.uint8).reshape(n, 50)
    tri = np.frombuffer(body[:, 12:48].tobytes(), dtype='<f4').reshape(n, 3, 3).astype(np.float64)
    return tri

def write_stl(path, tri, name):
    tri = np.asarray(tri, dtype=np.float64)
    e1 = tri[:, 1] - tri[:, 0]; e2 = tri[:, 2] - tri[:, 0]
    nrm = np.cross(e1, e2); ln = np.linalg.norm(nrm, axis=1, keepdims=True); ln[ln == 0] = 1.0
    nrm = (nrm / ln).astype('<f4')
    rec = np.zeros(len(tri), dtype=[('n', '<f4', 3), ('v', '<f4', (3, 3)), ('a', '<u2')])
    rec['n'] = nrm; rec['v'] = tri.astype('<f4')
    hdr = (f'EduBotics v1-urdf {name}'.encode('ascii') + b' ' * 80)[:80]
    with open(path, 'wb') as f:
        f.write(hdr); f.write(struct.pack('<I', len(tri))); f.write(rec.tobytes())

def components(tri):
    """Connected components by shared EDGES (faces that share two exact vertices),
    i.e. trimesh.split semantics: touching bodies that only share vertices stay apart."""
    v = tri.reshape(-1, 3)
    _, inv = np.unique(v, axis=0, return_inverse=True)
    f = inv.reshape(-1, 3)
    nf = len(f)
    parent = np.arange(nf)
    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]; a = parent[a]
        return a
    edges = np.concatenate([np.sort(f[:, [0, 1]], 1), np.sort(f[:, [1, 2]], 1), np.sort(f[:, [0, 2]], 1)])
    fid = np.concatenate([np.arange(nf)] * 3)
    order = np.lexsort((edges[:, 1], edges[:, 0]))
    e = edges[order]; fo = fid[order]
    # manifold edges only (exactly two faces): a contact seam where two bodies touch is
    # shared by four faces and must NOT join them (trimesh.split semantics)
    uk, start, cnt = np.unique(e, axis=0, return_index=True, return_counts=True)
    for s0 in start[cnt == 2]:
        ra, rb = find(fo[s0]), find(fo[s0 + 1])
        if ra != rb: parent[rb] = ra
    roots = np.array([find(i) for i in range(nf)])
    return [tri[roots == r] for r in np.unique(roots)]

def step_to_tri_m(step_path):
    import gmsh
    gmsh.initialize(); gmsh.option.setNumber('General.Terminal', 0)
    gmsh.model.occ.importShapes(step_path); gmsh.model.occ.synchronize()
    gmsh.option.setNumber('Mesh.MeshSizeMax', 2.0)
    gmsh.option.setNumber('Mesh.MeshSizeFromCurvature', 24)
    gmsh.model.mesh.generate(2)
    out = os.path.join(os.environ.get('TMPDIR', '/tmp'), 'v1_base_part2_mm.stl')
    gmsh.option.setNumber('Mesh.Binary', 1); gmsh.write(out); gmsh.finalize()
    tri_mm = read_stl(out)
    tri_b = tri_mm @ STEP_R.T + STEP_T_MM
    return tri_b / 1000.0

def decimate(tri, voxel_m):
    """Vertex clustering, MEAN representative: every output vertex is a mean of input
    vertices, so the bounding box can only shrink (never grow)."""
    if voxel_m <= 0: return tri
    v = tri.reshape(-1, 3)
    key = np.floor(v / voxel_m).astype(np.int64)
    uk, inv = np.unique(key, axis=0, return_inverse=True)
    sums = np.zeros((len(uk), 3)); np.add.at(sums, inv, v)
    cnt = np.bincount(inv, minlength=len(uk)).astype(float)
    rep = sums / cnt[:, None]
    f = inv.reshape(-1, 3)
    keep = (f[:, 0] != f[:, 1]) & (f[:, 1] != f[:, 2]) & (f[:, 0] != f[:, 2])
    f = f[keep]
    fs = np.sort(f, axis=1); _, first = np.unique(fs, axis=0, return_index=True)
    f = f[np.sort(first)]
    return rep[f]

URDF_EDITS = [  # (old, new) — each must occur EXACTLY once in the CRLF-normalised export
    ('<robot\n  name="5dof_assembly_urdf2">', '<robot\n  name="v1_studio">'),
    ('xyz="0.006294 0.00047303 0.014636"', 'xyz="0.006294 0.00047303 0.020636"'),
    ('xyz="0 0 0.0442"', 'xyz="0 0 0.0502"'),
    # joint1
    ('rpy="0 0 -1.5708" />\n    <parent\n      link="base_link" />\n    <child\n      link="link1" />\n    <axis\n      xyz="0 0 -1" />\n    <limit\n      lower="-1.57"\n      upper="1.57"',
     'rpy="0 0 -1.5708" />\n    <parent\n      link="base_link" />\n    <child\n      link="link1" />\n    <axis\n      xyz="0 0 -1" />\n    <limit\n      lower="-1.5708"\n      upper="1.5708"'),
    # joint2 / joint3 (two identical blocks -> handled by count below)
    # joint4
    ('lower="-1.92"\n      upper="1.92"', 'lower="-1.9198"\n      upper="1.9198"'),
    # joint5
    ('<child\n      link="link5" />\n    <axis\n      xyz="0 0 -1" />\n    <limit\n      lower="-1.57"\n      upper="1.57"',
     '<child\n      link="link5" />\n    <axis\n      xyz="0 0 -1" />\n    <limit\n      lower="-1.5708"\n      upper="1.5708"'),
    # RL_joint
    ('<axis\n      xyz="0 0 1" />\n    <limit\n      lower="0"\n      upper="1.57"', '<axis\n      xyz="0 0 1" />\n    <limit\n      lower="0"\n      upper="1.5708"'),
    # LF_joint
    ('<limit\n      lower="-1.57"\n      upper="0"', '<limit\n      lower="-1.5708"\n      upper="0"'),
    # claw gears: Spurgear_18T on RL (driven), 19T on LF, pivots 18.501 mm apart (module-1 direct mesh)
    ('multiplier="-1"', 'multiplier="-0.947368"'),
]
TWICE = ('lower="0"\n      upper="3.14"', 'lower="0"\n      upper="3.1416"')   # joint2 + joint3

HEADER = '''<!-- EduBotics in-repo copy of the V1 (5dof_assembly_urdf2, SolidWorks export 2026-10-22,
     hand-edited claw limits + LF <mimic>) with Base_part.2 as the base. Generated by
     tools/build_v1_urdf_assets.py; see that file for every edit. Deliberate SEMANTIC differences
     from the export: (1) robot name -> v1_studio; (2) mesh paths -> ./meshes/X;
     (3) right-angle limits 1.57 -> 1.5708, 3.14 -> 3.1416, joint4 1.92 -> 1.9198 (110 deg,
     rounded INWARD); (4) base_link frame RE-ORIGINED to the base bottom (the table plane):
     joint1 z 0.0442 -> 0.0502, base_link inertial z +0.006, every base_link mesh vertex +6 mm;
     (5) base_link mesh = Base_part.2.step (placed by the proven feature transform) + the
     exported joint-1 servo + top cover; the old Base_part-1 body and the adapter PCB are dropped. RL_joint/LF_joint are kept as
     exported (0 = closed, + = open) - no axis flip; LF <mimic> multiplier -1 -> -0.947368 (-18/19: the
     18T/19T claw gears, pivots 18.501 mm apart). Non-semantic: CRLF -> LF,
     this comment, meshes decimated by vertex clustering (mean representative: bounding boxes
     can only shrink). -->
'''

def build_urdf(src_text):
    t = src_text.replace('\r\n', '\n')
    for old, new in URDF_EDITS:
        assert t.count(old) == 1, (t.count(old), old)
        t = t.replace(old, new)
    assert t.count(TWICE[0]) == 2, t.count(TWICE[0])
    t = t.replace(TWICE[0], TWICE[1])
    assert t.count('package://5dof_assembly_urdf2/meshes/') == 18
    t = t.replace('package://5dof_assembly_urdf2/meshes/', './meshes/')
    head, rest = t.split('<robot', 1)
    return head + HEADER + '<robot' + rest

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('cad'); ap.add_argument('step'); ap.add_argument('out')
    ap.add_argument('--voxel-mm', type=float, default=0.5)
    a = ap.parse_args()
    meshes_in = os.path.join(a.cad, 'meshes'); out_m = os.path.join(a.out, 'meshes')
    os.makedirs(out_m, exist_ok=True)
    vox = a.voxel_mm / 1000.0
    # base_link
    old = read_stl(os.path.join(meshes_in, 'base_link.STL'))
    comps = components(old)
    def bnds(c):
        p = c.reshape(-1, 3); return np.array([p.min(0), p.max(0)])
    hit = [i for i, c in enumerate(comps) if np.allclose(bnds(c), OLD_BASE_PART_BOUNDS_M, atol=1e-5)]
    assert len(hit) == 1, hit
    # Owner decision Q1: keep ONLY the joint-1 servo and the top cover; drop the old base body
    # and the Waveshare adapter PCB with all its parts (every remaining component, y >= 26.99 mm).
    keep = [c for c in comps if any(np.allclose(bnds(c), b, atol=1e-5) for b in KEEP_BOUNDS_M)]
    assert len(keep) == 2, len(keep)
    dropped = [c for i, c in enumerate(comps) if i != hit[0] and not any(c is k for k in keep)]
    assert all(bnds(c)[0][1] >= 0.0269 for c in dropped), 'a dropped part is not on the PCB side'
    base = np.concatenate([step_to_tri_m(a.step)] + keep)
    base = base + np.array([0, 0, TABLE_DZ_M])
    write_stl(os.path.join(out_m, 'base_link.STL'), decimate(base, vox), 'base_link')
    for name in ('link1', 'link2', 'link3', 'link4', 'link5', 'end_effector', 'right_finger', 'left_finger'):
        tri = read_stl(os.path.join(meshes_in, name + '.STL'))
        write_stl(os.path.join(out_m, name + '.STL'), decimate(tri, vox), name)
    src = open(os.path.join(a.cad, 'urdf', '5dof_assembly_urdf2.urdf'), encoding='ascii').read()
    with open(os.path.join(a.out, 'v1.urdf'), 'w', encoding='utf-8', newline='\n') as f:
        f.write(build_urdf(src))

if __name__ == '__main__':
    main()
