#!/usr/bin/env python3
"""Per-link FULL-RESOLUTION extents of the V1, the fixture ``V1_LINK_BOXES`` is checked against.

Usage (any Python with numpy; the full-resolution assets come from the generator with
``--voxel-mm 0``, which skips the decimation):
  python tools/build_v1_urdf_assets.py <CAD export dir> <Base_part.2.step> <dir> --voxel-mm 0
  python tools/v1_fullres_link_extents.py <dir> \
      > physical_ai_tools/physical_ai_server/test/fixtures/v1_fullres_link_extents.json

Why it exists: the shipped meshes under ``public/v1-urdf/`` are decimated (1 mm vertex
clustering, mean representative), so their bounding boxes can only be SMALLER than the
full-resolution meshes the boxes in ``arm_geometry.V1_LINK_BOXES`` were derived from. A box
checked against the shipped meshes alone could therefore shrink below the real part and still
pass; ``test_arm_geometry_v1.py`` checks it against these extents too.

What it writes, per box index of ``V1_LINK_BOXES``: the axis-aligned (min, max) of that link's
full-resolution mesh in the link's own frame. Index 5 is the union, in link5's frame, of link5,
end_effector and both fingers over the whole claw window [0, 1.5708] at BOTH claw gear ratios
(the URDF's ``LF_joint`` mimic multiplier and -1). Each finger turns about ONE fixed axis, so a
vertex's coordinate along any link5 axis is A + B cos(phi) + C sin(phi) of its joint angle phi,
and its extreme over the window is taken in closed form (the stationary angle when it lies in the
window, else an end), not sampled. No value is rounded: links 0-4 are the mesh's own float32
coordinates, index 5 is exact to float64 rounding. Deterministic: same inputs -> same bytes."""
import json, math, os, struct, sys
import xml.etree.ElementTree as ET

import numpy as np

BOX_LINKS = ['base_link', 'link1', 'link2', 'link3', 'link4', 'link5']
GRIPPER_CHILDREN = ['end_effector', 'right_finger', 'left_finger']
CLAW_WINDOW = (0.0, 1.5708)


def read_stl_vertices(path):
    raw = open(path, 'rb').read()
    n = struct.unpack('<I', raw[80:84])[0]
    body = np.frombuffer(raw[84:84 + 50 * n], dtype=np.uint8).reshape(n, 50)
    pts = np.frombuffer(body[:, 12:48].tobytes(), dtype='<f4').reshape(-1, 3)
    return np.unique(pts.astype(np.float64), axis=0)


def rpy_matrix(r, p, y):
    cr, sr, cp, sp, cy, sy = (math.cos(r), math.sin(r), math.cos(p), math.sin(p),
                              math.cos(y), math.sin(y))
    return (np.array([[cy, -sy, 0], [sy, cy, 0], [0, 0, 1]])
            @ np.array([[cp, 0, sp], [0, 1, 0], [-sp, 0, cp]])
            @ np.array([[1, 0, 0], [0, cr, -sr], [0, sr, cr]]))


def axis_angle(axis, th):
    a = np.asarray(axis, float)
    norm = float(np.linalg.norm(a))
    if norm == 0.0:                     # a fixed joint carries axis "0 0 0"
        return np.eye(3)
    a = a / norm
    k = np.array([[0, -a[2], a[1]], [a[2], 0, -a[0]], [-a[1], a[0], 0]])
    return np.eye(3) + math.sin(th) * k + (1 - math.cos(th)) * (k @ k)


def load_urdf(path):
    root = ET.parse(path).getroot()
    joints, meshes = {}, {}
    for el in root.findall('joint'):
        o, ax, mim = el.find('origin'), el.find('axis'), el.find('mimic')
        joints[el.get('name')] = {
            'parent': el.find('parent').get('link'),
            'child': el.find('child').get('link'),
            'xyz': np.array([float(v) for v in o.get('xyz').split()]),
            'rpy': [float(v) for v in o.get('rpy').split()],
            'axis': ([float(v) for v in ax.get('xyz').split()] if ax is not None
                     else [0.0, 0.0, 1.0]),
            'mimic': (float(mim.get('multiplier', 1.0)) if mim is not None else None),
        }
    for link in root.findall('link'):
        for tag in ('collision', 'visual'):
            el = link.find(f'{tag}/geometry/mesh')
            if el is not None:
                meshes[link.get('name')] = el.get('filename').split('/')[-1]
                break
    return joints, meshes


def frames(joints, q):
    out = {'base_link': np.eye(4)}
    pending = dict(joints)
    while pending:
        progressed = False
        for name, j in list(pending.items()):
            if j['parent'] not in out:
                continue
            f = np.eye(4)
            f[:3, :3] = rpy_matrix(*j['rpy'])
            f[:3, 3] = j['xyz']
            r = np.eye(4)
            r[:3, :3] = axis_angle(j['axis'], q.get(name, 0.0))
            out[j['child']] = out[j['parent']] @ f @ r
            del pending[name]
            progressed = True
        assert progressed, f'unresolvable joint tree: {list(pending)}'
    return out


def arc_extents(g, axis, verts, phi_lo, phi_hi):
    """(min, max) per coordinate of ``g @ Rot(axis, phi) @ v`` over phi in [phi_lo, phi_hi]
    and every vertex v, in closed form: coordinate = A + R cos(phi - phi_k)."""
    a = np.asarray(axis, float) / np.linalg.norm(axis)
    along = verts @ a
    rot, off = g[:3, :3], g[:3, 3]
    big_a = (np.outer(along, a)) @ rot.T + off
    big_b = (verts - np.outer(along, a)) @ rot.T
    big_c = np.cross(np.broadcast_to(a, verts.shape), verts) @ rot.T
    amp = np.hypot(big_b, big_c)
    peak = np.arctan2(big_c, big_b)
    span = phi_hi - phi_lo

    def inside(angle):
        return np.mod(angle - phi_lo, 2.0 * np.pi) <= span

    ends = [big_a + big_b * math.cos(t) + big_c * math.sin(t) for t in (phi_lo, phi_hi)]
    hi = np.where(inside(peak), big_a + amp, np.maximum(*ends))
    lo = np.where(inside(peak + np.pi), big_a - amp, np.minimum(*ends))
    return lo.min(0), hi.max(0)


def main():
    src = sys.argv[1]
    joints, meshes = load_urdf(os.path.join(src, 'v1.urdf'))
    verts = {name: read_stl_vertices(os.path.join(src, 'meshes', meshes[name]))
             for name in BOX_LINKS + GRIPPER_CHILDREN}
    boxes = []
    for link in BOX_LINKS[:5]:
        pts = verts[link]
        boxes.append((pts.min(0), pts.max(0)))
    lf_multipliers = (joints['LF_joint']['mimic'], -1.0)
    zero = frames(joints, {})
    inv5 = np.linalg.inv(zero['link5'])
    lo5 = verts['link5'].min(0)
    hi5 = verts['link5'].max(0)
    ee = inv5 @ zero['end_effector']               # fixed to link5 (end_joint)
    pts = verts['end_effector'] @ ee[:3, :3].T + ee[:3, 3]
    lo5, hi5 = np.minimum(lo5, pts.min(0)), np.maximum(hi5, pts.max(0))
    for multiplier in lf_multipliers:
        for child, joint, scale in (('right_finger', 'RL_joint', 1.0),
                                    ('left_finger', 'LF_joint', multiplier)):
            ends = sorted((scale * CLAW_WINDOW[0], scale * CLAW_WINDOW[1]))
            lo, hi = arc_extents(inv5 @ zero[child], joints[joint]['axis'],
                                 verts[child], *ends)
            lo5, hi5 = np.minimum(lo5, lo), np.maximum(hi5, hi)
    boxes.append((lo5, hi5))
    doc = {
        'what': ('Per-box-index (min, max) of the V1 full-resolution meshes in each link frame; '
                 'index 5 = link5 + end_effector + both fingers over the claw window [0, 1.5708] '
                 'at the LF mimic multipliers listed, in closed form. Not rounded.'),
        'regenerate': ('python tools/build_v1_urdf_assets.py <CAD export dir> <Base_part.2.step> '
                       '<dir> --voxel-mm 0 && python tools/v1_fullres_link_extents.py <dir> '
                       '> physical_ai_tools/physical_ai_server/test/fixtures/'
                       'v1_fullres_link_extents.json'),
        'links': BOX_LINKS,
        'lf_multipliers': list(lf_multipliers),
        'boxes': [[[float(v) for v in lo], [float(v) for v in hi]] for lo, hi in boxes],
    }
    json.dump(doc, sys.stdout, indent=1)
    sys.stdout.write('\n')


if __name__ == '__main__':
    main()
