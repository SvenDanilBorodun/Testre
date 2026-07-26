"""Independent re-verification of the edu6 solver contracts the doc claims.

Claims under test (zero trust — measured here, not read from the tests):
 C1  fold bound: max |q6| == 90.0000 deg over a full 360 deg tag sweep
 C2  solve() ignores `seed` entirely (the reverted seed-relative fold)
 C3  chained grasps never drift toward the +-180 deg map edge
 C4  roll_from_joints round-trips inside the fold window, and is the IDENTITY on OMX
 C5  the jaw azimuth is preserved mod pi by the fold (same physical grasp)
 C6  worst wrist swing between two grasps 2 deg apart in tag yaw: fold vs no fold
 C7  reach: solvable set + the profile's advertised reach_inner/outer
"""
import math
import sys

sys.path.insert(0, r"C:\Users\svend\newaarm\Testre\physical_ai_tools\physical_ai_server")

from physical_ai_server.workflow import edu6_ik  # noqa: E402
from physical_ai_server.workflow import ik_solver as omx_ik  # noqa: E402

S = edu6_ik.Edu6IKSolver()
GRASP_ROLL = math.radians(90.0)


def wrap(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


# ---------- C1 + C5: fold bound and jaw azimuth ----------
worst = 0.0
azim_bad = []
Z = 0.015
pts = []
for r_mm in range(95, 211, 5):
    for bearing_deg in range(-90, 91, 10):
        r = r_mm / 1000.0
        b = math.radians(bearing_deg)
        pts.append((edu6_ik.BASE_AXIS_X_WORLD + r * math.cos(b), r * math.sin(b), Z))

solved = 0
for (x, y, z) in pts:
    for tag_deg in range(0, 360, 2):
        tag = math.radians(tag_deg)
        roll = S.base_yaw(x, y) - tag + GRASP_ROLL
        q = S.solve((x, y, z), roll=roll)
        if q is None:
            continue
        solved += 1
        worst = max(worst, abs(q[5]))
        # jaw azimuth must equal the unfolded choice mod pi
        unfolded = wrap(math.pi - roll)
        if abs(wrap(q[5] - unfolded)) > 1e-9 and abs(abs(wrap(q[5] - unfolded)) - math.pi) > 1e-9:
            azim_bad.append((x, y, tag_deg, q[5], unfolded))

print(f"C1  solved targets={solved}  max |q6| = {math.degrees(worst):.6f} deg"
      f"   -> {'OK' if worst <= math.pi / 2 + 1e-12 else 'FAIL'}")
print(f"C5  jaw-azimuth-preserved-mod-pi violations = {len(azim_bad)}"
      f"   -> {'OK' if not azim_bad else 'FAIL'}")

# ---------- C2: seed must not change the answer ----------
seeds = [None,
         [0.0] * 6,
         [0.3, 0.8, -1.9, 0.0, 0.8, math.radians(140)],
         [0.0, 0.8, -1.9, 0.0, 0.8, math.radians(-179)],
         [0.0, 0.8, -1.9, 0.0, 0.8, math.pi],
         [0.0, 0.8, -1.9, 0.0, 0.8, -math.pi]]
diff = 0
maxq6_seeded = 0.0
for (x, y, z) in pts[::7]:
    for tag_deg in range(0, 360, 15):
        roll = S.base_yaw(x, y) - math.radians(tag_deg) + GRASP_ROLL
        base = S.solve((x, y, z), roll=roll)
        if base is None:
            continue
        for sd in seeds:
            got = S.solve((x, y, z), roll=roll, seed=sd)
            if got is None or any(abs(a - b) > 1e-12 for a, b in zip(base, got)):
                diff += 1
            if got:
                maxq6_seeded = max(maxq6_seeded, abs(got[5]))
print(f"C2  seed-dependent answers = {diff}   max |q6| across all seeds = "
      f"{math.degrees(maxq6_seeded):.4f} deg   -> {'OK' if diff == 0 and maxq6_seeded <= math.pi/2+1e-12 else 'FAIL'}")

# ---------- C3: chained grasps, feed each answer back as the next seed ----------
import random  # noqa: E402
rnd = random.Random(20260726)
cur = [0.0, 0.70, -2.40, 0.0, 0.70, 0.0]
peak = 0.0
swings = []
for _ in range(4000):
    r = rnd.uniform(0.095, 0.205)
    b = math.radians(rnd.uniform(-85, 85))
    x = edu6_ik.BASE_AXIS_X_WORLD + r * math.cos(b)
    y = r * math.sin(b)
    roll = S.base_yaw(x, y) - rnd.uniform(-math.pi, math.pi) + GRASP_ROLL
    q = S.solve((x, y, Z), roll=roll, seed=cur)
    if q is None:
        continue
    swings.append(abs(q[5] - cur[5]))
    cur = q
    peak = max(peak, abs(q[5]))
print(f"C3  chained {len(swings)} grasps: peak |q6| = {math.degrees(peak):.4f} deg, "
      f"worst swing = {math.degrees(max(swings)):.1f} deg, mean = "
      f"{math.degrees(sum(swings)/len(swings)):.1f} deg   -> "
      f"{'OK' if peak <= math.pi/2 + 1e-12 else 'FAIL'}")

# ---------- C4: roll_from_joints round-trip + OMX identity ----------
bad_rt = []
for (x, y, z) in pts[::11]:
    for tag_deg in range(0, 360, 20):
        roll = S.base_yaw(x, y) - math.radians(tag_deg) + GRASP_ROLL
        q = S.solve((x, y, z), roll=roll)
        if q is None:
            continue
        back = S.roll_from_joints(q)
        q2 = S.solve((x, y, z), roll=back)
        if q2 is None or abs(wrap(q2[5] - q[5])) > 1e-9:
            bad_rt.append((x, y, tag_deg, q[5], back, None if q2 is None else q2[5]))
print(f"C4a edu6 roll_from_joints round-trip failures = {len(bad_rt)}"
      f"   -> {'OK' if not bad_rt else 'FAIL'}")

O = omx_ik.IKSolver() if hasattr(omx_ik, 'IKSolver') else None
if O is None:
    cand = [n for n in dir(omx_ik) if 'Solver' in n]
    print("   OMX solver class candidates:", cand)
    O = getattr(omx_ik, cand[0])()
worst_id = 0.0
for deg in range(-180, 181):
    j = [0.0, 0.0, 0.0, 0.0, math.radians(deg)]
    got = O.roll_from_joints(j)
    worst_id = max(worst_id, abs(wrap(got - j[4])))
print(f"C4b OMX roll_from_joints identity deviation = {worst_id:.3e} rad"
      f"   -> {'OK' if worst_id < 1e-12 else 'FAIL'}")

# ---------- C6: worst swing between neighbouring tag yaws, fold vs no fold ----------
x0, y0 = edu6_ik.BASE_AXIS_X_WORLD + 0.16, 0.0
fold_max = nofold_max = 0.0
for tag_deg in range(0, 360):
    r1 = S.base_yaw(x0, y0) - math.radians(tag_deg) + GRASP_ROLL
    r2 = S.base_yaw(x0, y0) - math.radians(tag_deg + 2) + GRASP_ROLL
    a = S.solve((x0, y0, Z), roll=r1)
    b2 = S.solve((x0, y0, Z), roll=r2)
    if a and b2:
        fold_max = max(fold_max, abs(a[5] - b2[5]))
    u1, u2 = wrap(math.pi - r1), wrap(math.pi - r2)
    nofold_max = max(nofold_max, abs(u1 - u2))
print(f"C6  worst wrist swing over a 2 deg tag step: fold={math.degrees(fold_max):.1f} deg  "
      f"no-fold={math.degrees(nofold_max):.1f} deg")

# ---------- C7: reach vs the advertised profile numbers ----------
sys.path.insert(0, r"C:\Users\svend\newaarm\Testre\physical_ai_tools\physical_ai_server")
from physical_ai_server import robot_profiles  # noqa: E402
p = robot_profiles.resolve('edu6_studio')
print(f"C7  profile reach_inner_m={getattr(p, 'reach_inner_m', None)} "
      f"reach_outer_m={getattr(p, 'reach_outer_m', None)}  n={p.num_arm_joints}")
lo = hi = None
for mm in range(40, 300):
    r = mm / 1000.0
    x = edu6_ik.BASE_AXIS_X_WORLD + r
    q = S.solve((x, 0.0, Z), roll=GRASP_ROLL)
    if q is not None:
        lo = r if lo is None else lo
        hi = r
print(f"    measured straight-ahead solvable radius at z={Z}: {lo:.3f} .. {hi:.3f} m")
