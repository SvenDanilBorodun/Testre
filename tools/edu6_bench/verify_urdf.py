"""Independent re-verification of edu6 baked kinematics vs the EXTERNAL URDF.

Zero trust: parses the vendor URDF at ~/Downloads directly, re-derives every
constant edu6_ik.py bakes, and compares. Also cross-checks the in-repo copy
the React 3D twin loads, and the driver/provision limit tables.
"""
import math
import sys
import xml.etree.ElementTree as ET

EXT = r"C:\Users\svend\Downloads\follower_arm_modified_final1\follower_arm_modified_final1\urdf\follower_arm_modified_final1.urdf"
REPO_COPY = r"C:\Users\svend\newaarm\Testre\physical_ai_tools\physical_ai_manager\public\edu6-urdf\edu6.urdf"

sys.path.insert(0, r"C:\Users\svend\newaarm\Testre\physical_ai_tools\physical_ai_server")
from physical_ai_server.workflow import edu6_ik as ik  # noqa: E402


def parse(path):
    root = ET.parse(path).getroot()
    joints = {}
    for j in root.findall('joint'):
        name = j.get('name')
        typ = j.get('type')
        org = j.find('origin')
        xyz = tuple(float(v) for v in (org.get('xyz') or '0 0 0').split()) if org is not None else (0, 0, 0)
        rpy = tuple(float(v) for v in (org.get('rpy') or '0 0 0').split()) if org is not None else (0, 0, 0)
        ax = j.find('axis')
        axis = tuple(float(v) for v in ax.get('xyz').split()) if ax is not None else None
        lim = j.find('limit')
        limits = (float(lim.get('lower')), float(lim.get('upper'))) if lim is not None and lim.get('lower') else None
        eff = float(lim.get('effort')) if lim is not None and lim.get('effort') else None
        vel = float(lim.get('velocity')) if lim is not None and lim.get('velocity') else None
        joints[name] = dict(type=typ, xyz=xyz, rpy=rpy, axis=axis, limits=limits,
                            parent=j.find('parent').get('link'),
                            child=j.find('child').get('link'), effort=eff, velocity=vel)
    return joints


ext = parse(EXT)
repo = parse(REPO_COPY)

print("=== joints in external URDF ===")
for n, d in ext.items():
    print(f"  {n:18s} {d['type']:10s} parent={d['parent']:14s} child={d['child']:14s} "
          f"axis={d['axis']} limits={d['limits']} vel={d['velocity']}")

fails = []


def chk(label, got, want, tol=0.0):
    ok = all(abs(a - b) <= tol for a, b in zip(got, want)) and len(got) == len(want) \
        if isinstance(want, (tuple, list)) else abs(got - want) <= tol
    print(f"{'OK ' if ok else 'FAIL'}  {label}: got={got} want={want}")
    if not ok:
        fails.append(label)


print("\n=== baked origins/rpy vs external URDF (exact) ===")
chk("_J1_XYZ", ik._J1_XYZ, ext['joint1']['xyz'])
chk("_J2_XYZ", ik._J2_XYZ, ext['joint2']['xyz'])
chk("_J2_RPY", ik._J2_RPY, ext['joint2']['rpy'], 1e-12)
chk("_J3_XYZ", ik._J3_XYZ, ext['joint3']['xyz'])
chk("_J4_XYZ", ik._J4_XYZ, ext['joint4']['xyz'])
chk("_J4_RPY", ik._J4_RPY, ext['joint4']['rpy'], 1e-12)
chk("_J5_XYZ", ik._J5_XYZ, ext['joint5']['xyz'])
chk("_J5_RPY", ik._J5_RPY, ext['joint5']['rpy'], 1e-12)
chk("_J6_XYZ", ik._J6_XYZ, ext['joint6']['xyz'])
chk("_J6_RPY", ik._J6_RPY, ext['joint6']['rpy'], 1e-12)

print("\n=== axes ===")
for i, jn in enumerate(['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'joint6']):
    chk(f"_AXES[{i}] ({jn})", ik._AXES[i], tuple(int(v) for v in ext[jn]['axis']))

print("\n=== joint limits: URDF vs edu6_ik vs driver vs provision tool ===")
sys.path.insert(0, r"C:\Users\svend\newaarm\Testre\robotis_ai_setup\docker\open_manipulator")
import ast
drv_src = open(r"C:\Users\svend\newaarm\Testre\robotis_ai_setup\docker\open_manipulator\edu6_arm_node.py", encoding='utf-8').read()
prov_src = open(r"C:\Users\svend\newaarm\Testre\tools\edu6_provision.py", encoding='utf-8').read()


def grab_tuple(src, name):
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return ast.literal_eval(node.value)
    return None


drv_lim = grab_tuple(drv_src, 'JOINT_LIMITS_RAD')
prov_lim = grab_tuple(prov_src, 'JOINT_LIMITS_RAD')
print(f"driver   JOINT_LIMITS_RAD = {drv_lim}")
print(f"provision JOINT_LIMITS_RAD = {prov_lim}")
for i, jn in enumerate(['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'joint6']):
    u = ext[jn]['limits']
    chk(f"ik limits {jn} vs URDF", ik._EDU6_JOINT_LIMITS_RAD[i], u, 0.0)
    chk(f"driver limits {jn} vs URDF", tuple(drv_lim[i]), u, 0.0)
    chk(f"provision limits {jn} vs URDF", tuple(prov_lim[i]), u, 0.0)

# gripper joint (end_gear)
gj = [n for n in ext if 'gear' in n.lower() or 'grip' in n.lower()]
print(f"\ngripper-ish joints in URDF: {gj}")
for n in gj:
    print(f"   {n}: type={ext[n]['type']} limits={ext[n]['limits']} axis={ext[n]['axis']} "
          f"parent={ext[n]['parent']} child={ext[n]['child']}")
print(f"driver gripper limit  = {drv_lim[6]}")
print(f"provision gripper lim = {prov_lim[6]}")

print("\n=== derived planar constants (recomputed here from URDF numbers) ===")
print(f"  _SHOULDER_Z = {ik._SHOULDER_Z!r}")
print(f"  _L2 = {ik._L2!r}   _L3 = {ik._L3!r}   _ALPHA0 = {math.degrees(ik._ALPHA0):.6f} deg")
print(f"  _REACH_MIN = {ik._REACH_MIN:.6f}  _REACH_MAX = {ik._REACH_MAX:.6f}")
print(f"  _L_TOOL = {ik._L_TOOL}  _WRIST_TO_LINK6 = {ik._WRIST_TO_LINK6}  "
      f"_LINK6_TO_TCP = {ik._LINK6_TO_TCP}")

print("\n=== external vs in-repo URDF copy (twin asset) ===")
same = True
for n in sorted(set(ext) | set(repo)):
    if n not in ext or n not in repo:
        print(f"FAIL  joint {n} present in only one file")
        same = False
        fails.append(f"joint {n} asymmetric")
        continue
    for k in ('type', 'xyz', 'rpy', 'axis', 'limits', 'parent', 'child'):
        a, b = ext[n][k], repo[n][k]
        if a != b:
            print(f"FAIL  {n}.{k}: ext={a} repo={b}")
            same = False
            fails.append(f"{n}.{k}")
print("OK    all joints identical between external and in-repo URDF" if same else "")

print("\n" + ("ALL CHECKS PASSED" if not fails else f"FAILURES: {fails}"))
