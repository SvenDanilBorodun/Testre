"""Shared Blockly-JSON validator used by both the student
``/workflows`` router and the teacher template router.

Extracted from ``routes/workflows.py`` after the audit found that the
teacher endpoint at ``routes/teacher.py`` was inserting the workflow
without size or depth checks (audit §2.1) — every Blockly write path
must call ``validate_blockly_json`` before touching Postgres.

The size + depth caps are kept as OOM / stack-overflow protections.
The block-type allowlist was removed in the safety-stripdown so a
workflow referencing a new block doesn't get rejected at the cloud API
gate; the ROS server interprets whatever it sees.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

from fastapi import HTTPException


MAX_BLOCKLY_JSON_BYTES = 256 * 1024
MAX_BLOCKLY_DEPTH = 64
MAX_NAME_LENGTH = 100

# Roboter Studio Phase-3 Sim-Szene. Much smaller than blockly_json — it's a
# flat list of placed catalog objects ({type, tag_id, x, y, yaw}). 64 KB is
# generous (hundreds of objects) while still bounding a malicious payload
# before it reaches Postgres.
MAX_SIM_SCENE_JSON_BYTES = 64 * 1024
# Defense-in-depth object-count cap (parity with the server-side _MAX_SIM_OBJECTS):
# a ≤64 KB scene could still carry hundreds of entries; bound the count so a junk
# scene never reaches Postgres / the ROS sim resolve loop.
MAX_SIM_SCENE_OBJECTS = 64
# Phase-4 No-Go zones (axis-aligned base-frame keep-out boxes, persisted in
# sim_scene.zones). Same defense-in-depth count cap as the objects list.
MAX_SIM_SCENE_ZONES = 16
# Phase-2 Tempo: the optional global speed multiplier persisted in
# sim_scene.tempo. The window MUST match the ROS server's
# handlers/motion.py ``_TEMPO_MIN`` / ``_TEMPO_MAX`` (the cloud API can't import
# the physical_ai_server package — separate deployment — so the bounds are
# duplicated here, like MAX_SIM_SCENE_OBJECTS mirrors the server-side cap).
TEMPO_MIN = 0.5
TEMPO_MAX = 2.0

# Batch-2b recorded replay trajectories (CONTRACT B):
#   { "fps": <number>, "points": [ [j1..jn, grip, t_s], ... ] }
# validate_trajectory guards the `points` list (a hand-guided recording) before
# it hits Postgres. 5000 points at 30 fps is ~166 s of motion — far beyond any
# real hand-guided demo, so it is a hard OOM ceiling, not a UX limit; the JSON
# byte cap (mirrors the blockly cap) is the binding guard for adversarial input.
MAX_TRAJECTORY_POINTS = 5000
MAX_TRAJECTORY_JSON_BYTES = 256 * 1024
# Each point is exactly (num_arm_joints + 2) finite numbers: joints + gripper +
# t_seconds. The width follows the ARM FAMILY the recording was made on — the
# `robot_profile` column (migration 035): 'omx_f' (5 arm joints → 7-wide, the
# pre-edu6 default for untagged rows/clients) or 'edu6_studio' (6 → 8-wide).
# The id set mirrors the server registry's data_robot_type values — duplicated
# here like TEMPO_MIN/MAX (the cloud cannot import the server package).
TRAJECTORY_POINT_LEN = 7
TRAJECTORY_ROBOT_PROFILES: dict = {
    "omx_f": 7,
    "edu6_studio": 8,
}
# Sanity ceiling on the recording rate. The recorder runs at 20-30 Hz; a value
# past this is a client bug and would make replay timing nonsensical.
TRAJECTORY_FPS_MAX = 1000.0
# Postgres INTEGER (int4) upper bound. point_count is stored in an INTEGER column
# (migration 034); a body value past this overflows the column and raises an
# uncaught 500 on INSERT, so it is rejected at the gate instead.
INT4_MAX = 2_147_483_647

# Recorded-trajectory name charset + length. MUST mirror the ROS backend
# (`_DESTINATION_NAME_RE` in physical_ai_server/workflow/handlers/destinations.py)
# and the Blockly frontend: after trimming, a name is 1–40 chars of letters
# (incl. German umlauts), digits, spaces, underscore, hyphen. The cloud API can't
# import the physical_ai_server package (separate deployment), so the regex is
# duplicated here — like MAX_SIM_SCENE_OBJECTS / TEMPO_* mirror their server-side
# caps. Without this the cloud stored 41–100-char / whitespace-only / arbitrary
# names (emoji, control chars, `/`) that NO Blockly block can reference (dead
# storage) and that the ``by-name/{name}`` path route can't fetch (a `/` breaks
# the path).
TRAJECTORY_NAME_MAX_LENGTH = 40
TRAJECTORY_NAME_RE = re.compile(r"^[A-Za-zÄÖÜäöüß0-9 _\-]{1,40}$")


def validate_blockly_json(payload: dict) -> None:
    """Defang malicious or runaway payloads before they hit Postgres.

    Two cheap checks: total serialised size and nested depth. Real
    semantic validation (block types, color enums, class enums, math
    ranges) runs on the ROS server when ``StartWorkflow`` is called.
    """
    try:
        encoded = json.dumps(payload)
    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"Workflow-JSON ist ungültig: {e}")
    if len(encoded.encode("utf-8")) > MAX_BLOCKLY_JSON_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Workflow ist zu groß (>{MAX_BLOCKLY_JSON_BYTES // 1024} KB).",
        )

    # Off-by-one fix: a tree of EXACTLY MAX_BLOCKLY_DEPTH levels must be
    # accepted; one level deeper must be rejected. The depth count is the
    # number of nested container hops (dict/list); a scalar at the root
    # has depth 0, one nested dict has depth 1, etc. We early-bail as
    # soon as we observe depth > MAX_BLOCKLY_DEPTH so the recursion stays
    # bounded even on adversarial input that uses non-container values
    # to skip the predicate.
    def _depth(node: Any, current: int) -> int:
        if current > MAX_BLOCKLY_DEPTH:
            return current
        if isinstance(node, dict):
            return max((_depth(v, current + 1) for v in node.values()), default=current)
        if isinstance(node, list):
            return max((_depth(v, current + 1) for v in node), default=current)
        return current

    if _depth(payload, 0) >= MAX_BLOCKLY_DEPTH + 1:
        raise HTTPException(status_code=400, detail="Workflow ist zu tief verschachtelt.")


def validate_sim_scene(scene: dict) -> None:
    """Defang a malicious or runaway Sim-Szene before it hits Postgres.

    Mirrors ``validate_blockly_json``: a JSON-object type check plus a total
    serialised-size cap. The semantic shape (objects must sit inside the IK
    annulus, tag_ids unique, etc.) is enforced by the ROS sim runtime at
    placement / run time — the cloud gate only guards size + type.
    """
    if not isinstance(scene, dict):
        raise HTTPException(
            status_code=400, detail="Sim-Szene muss ein JSON-Objekt sein."
        )
    try:
        encoded = json.dumps(scene)
    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"Sim-Szene-JSON ist ungültig: {e}")
    if len(encoded.encode("utf-8")) > MAX_SIM_SCENE_JSON_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Sim-Szene ist zu groß (>{MAX_SIM_SCENE_JSON_BYTES // 1024} KB).",
        )
    objects = scene.get("objects")
    if objects is not None and not isinstance(objects, list):
        raise HTTPException(
            status_code=400, detail="Sim-Szene „objects“ muss eine Liste sein."
        )
    if isinstance(objects, list) and len(objects) > MAX_SIM_SCENE_OBJECTS:
        raise HTTPException(
            status_code=413,
            detail=f"Zu viele Objekte in der Sim-Szene (höchstens {MAX_SIM_SCENE_OBJECTS}).",
        )

    _validate_zones(scene.get("zones"))
    _validate_tempo(scene.get("tempo"))


def _validate_tempo(tempo: Any) -> None:
    """Validate the optional Phase-2 run-bar Tempo (global speed multiplier)
    persisted in ``sim_scene.tempo``.

    ``None`` (absent) is allowed. Otherwise it must be a finite number
    (``bool`` excluded — it is an ``int`` subclass and never a speed) inside the
    ``[TEMPO_MIN, TEMPO_MAX]`` window; ``json.loads`` accepts bare
    ``NaN``/``Infinity`` so a finite-check is required. German HTTP 400 on any
    violation. Defense-in-depth only — Tempo is a teaching speed knob on the
    workflow runtime, NOT a safety envelope; the cloud gate guards shape/range."""
    if tempo is None:
        return
    if isinstance(tempo, bool) or not isinstance(tempo, (int, float)):
        raise HTTPException(status_code=400, detail="Tempo muss eine Zahl sein.")
    if not math.isfinite(tempo):
        raise HTTPException(
            status_code=400, detail="Tempo muss eine endliche Zahl sein."
        )
    if tempo < TEMPO_MIN or tempo > TEMPO_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"Tempo muss zwischen {TEMPO_MIN} und {TEMPO_MAX} liegen.",
        )


def _validate_zones(zones: Any) -> None:
    """Validate the Phase-4 No-Go zone list (sim_scene.zones).

    Each zone is an axis-aligned base-frame box ``{"min": [x, y, z],
    "max": [x, y, z]}`` in metres with ``min[i] <= max[i]``. The cloud gate
    rejects non-list / over-count / malformed / non-finite corners before
    they reach Postgres or the ROS path-guard; ``json.loads`` accepts bare
    ``NaN``/``Infinity``, so a finite-check (``math.isfinite``) is required.
    ``bool`` is excluded — it is an ``int`` subclass and never a coordinate.
    """
    if zones is None:
        return
    if not isinstance(zones, list):
        raise HTTPException(
            status_code=400, detail="Sim-Szene „zones“ muss eine Liste sein."
        )
    if len(zones) > MAX_SIM_SCENE_ZONES:
        raise HTTPException(
            status_code=413,
            detail=f"Zu viele Sperrzonen in der Sim-Szene (höchstens {MAX_SIM_SCENE_ZONES}).",
        )
    for zone in zones:
        if not isinstance(zone, dict) or "min" not in zone or "max" not in zone:
            raise HTTPException(
                status_code=400,
                detail="Jede Sperrzone muss „min“ und „max“ enthalten.",
            )
        corner_min = _corner(zone["min"])
        corner_max = _corner(zone["max"])
        for lo, hi in zip(corner_min, corner_max):
            if lo > hi:
                raise HTTPException(
                    status_code=400,
                    detail="Sperrzone ungültig: „min“ muss kleiner oder gleich „max“ sein.",
                )


def _corner(value: Any) -> list[float]:
    """Coerce + validate a zone corner: a 3-element list of finite numbers
    (``bool`` excluded). Raises HTTP 400 in German on any shape violation."""
    if not isinstance(value, list) or len(value) != 3:
        raise HTTPException(
            status_code=400,
            detail="Sperrzone ungültig: „min“ und „max“ müssen je drei Zahlen sein.",
        )
    for component in value:
        if isinstance(component, bool) or not isinstance(component, (int, float)):
            raise HTTPException(
                status_code=400,
                detail="Sperrzone ungültig: Koordinaten müssen Zahlen sein.",
            )
        if not math.isfinite(component):
            raise HTTPException(
                status_code=400,
                detail="Sperrzone ungültig: Koordinaten müssen endliche Zahlen sein.",
            )
    return value


def validate_trajectory_name(name: Any) -> str:
    """Validate + trim a recorded-trajectory name and return the trimmed form.

    Mirrors the ROS backend (``_DESTINATION_NAME_RE``) and the Blockly frontend:
    after ``.strip()`` the name must match ``TRAJECTORY_NAME_RE`` (1–40 chars of
    letters incl. German umlauts, digits, spaces, ``_`` and ``-``). Rejects
    empty / whitespace-only / too-long / out-of-charset input with a German HTTP
    400. The caller MUST store the returned (trimmed) value so a leading/trailing
    space never survives to Postgres. Defense-in-depth against dead storage that
    no Blockly block can reference and against a ``/`` breaking the by-name path
    route — the ROS server is the semantic authority; the cloud gate mirrors it.
    """
    if not isinstance(name, str):
        raise HTTPException(status_code=400, detail="Ungültiger Aufnahme-Name.")
    trimmed = name.strip()
    if not trimmed:
        raise HTTPException(status_code=400, detail="Ungültiger Aufnahme-Name.")
    if not TRAJECTORY_NAME_RE.match(trimmed):
        # The regex already caps length at 40; a single message covers both the
        # too-long and the out-of-charset case (mirrors the frontend hint).
        raise HTTPException(
            status_code=400,
            detail=(
                "Der Aufnahme-Name darf höchstens 40 Zeichen lang sein und nur "
                "Buchstaben, Ziffern, Leerzeichen, _ und - enthalten."
            ),
        )
    return trimmed


def validate_trajectory(points: Any, fps: Any = None,
                        robot_profile: Any = None) -> None:
    """Defang a malicious or runaway recorded replay trajectory (CONTRACT B)
    before it hits Postgres.

    ``points`` must be a non-empty list of ≤ ``MAX_TRAJECTORY_POINTS`` entries,
    each a list of exactly ``point_len`` finite numbers — the width for the
    recording's arm family (``robot_profile``: see
    ``TRAJECTORY_ROBOT_PROFILES``; ``None``/absent = the 7-wide OMX default,
    what every pre-edu6 client sends). ``bool`` is excluded (an ``int``
    subclass, never a joint value); ``json.loads`` accepts bare
    ``NaN``/``Infinity`` so a finite-check is required. The total serialised
    size is capped at ``MAX_TRAJECTORY_JSON_BYTES`` (mirrors
    ``validate_blockly_json``). When ``fps`` is supplied it must be a positive
    finite number ≤ ``TRAJECTORY_FPS_MAX``. German HTTP 400/413 on any
    violation. Defense-in-depth only — replay is a teaching feature, not a
    safety envelope; the cloud gate guards shape/size.
    """
    point_len = validate_trajectory_robot_profile(robot_profile)
    if not isinstance(points, list):
        raise HTTPException(
            status_code=400, detail="Bewegungsdaten müssen eine Liste von Punkten sein."
        )
    if not points:
        raise HTTPException(
            status_code=400, detail="Bewegung enthält keine Punkte."
        )
    if len(points) > MAX_TRAJECTORY_POINTS:
        raise HTTPException(
            status_code=413,
            detail=f"Bewegung ist zu lang (höchstens {MAX_TRAJECTORY_POINTS} Punkte).",
        )
    try:
        encoded = json.dumps(points)
    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"Bewegungsdaten sind ungültig: {e}")
    if len(encoded.encode("utf-8")) > MAX_TRAJECTORY_JSON_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Bewegung ist zu groß (>{MAX_TRAJECTORY_JSON_BYTES // 1024} KB).",
        )
    for point in points:
        _trajectory_point(point, point_len)
    if fps is not None:
        _validate_fps(fps)


def validate_trajectory_robot_profile(robot_profile: Any) -> int:
    """Validate an optional trajectory ``robot_profile`` tag and return the
    point width it implies. ``None``/empty → the OMX default width (untagged
    pre-edu6 rows and clients). An unknown id → German HTTP 400 (the allowlist
    is the small ``TRAJECTORY_ROBOT_PROFILES`` map)."""
    if robot_profile is None or (isinstance(robot_profile, str)
                                 and not robot_profile.strip()):
        return TRAJECTORY_POINT_LEN
    if not isinstance(robot_profile, str)             or robot_profile.strip() not in TRAJECTORY_ROBOT_PROFILES:
        raise HTTPException(
            status_code=400,
            detail="Unbekanntes Roboterprofil für diese Bewegung.",
        )
    return TRAJECTORY_ROBOT_PROFILES[robot_profile.strip()]


def _trajectory_point(value: Any, point_len: int = TRAJECTORY_POINT_LEN) -> list[float]:
    """Validate one recorded sample: exactly ``point_len`` finite numbers
    (``bool`` excluded; 7 for OMX recordings, 8 for edu6 — joints + gripper +
    t_s). Raises HTTP 400 in German on any violation. Mirrors ``_corner`` but
    for the Contract-B point."""
    if not isinstance(value, list) or len(value) != point_len:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Bewegungspunkt ungültig: jeder Punkt muss {point_len} "
                "Zahlen enthalten."
            ),
        )
    for component in value:
        if isinstance(component, bool) or not isinstance(component, (int, float)):
            raise HTTPException(
                status_code=400,
                detail="Bewegungspunkt ungültig: Werte müssen Zahlen sein.",
            )
        if not math.isfinite(component):
            raise HTTPException(
                status_code=400,
                detail="Bewegungspunkt ungültig: Werte müssen endliche Zahlen sein.",
            )
    return value


def _validate_fps(fps: Any) -> None:
    """Validate the trajectory recording rate: a positive finite number
    (``bool`` excluded) not exceeding ``TRAJECTORY_FPS_MAX``. German HTTP 400."""
    if isinstance(fps, bool) or not isinstance(fps, (int, float)):
        raise HTTPException(status_code=400, detail="FPS muss eine Zahl sein.")
    if not math.isfinite(fps) or fps <= 0:
        raise HTTPException(
            status_code=400, detail="FPS muss eine positive endliche Zahl sein."
        )
    if fps > TRAJECTORY_FPS_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"FPS ist zu hoch (höchstens {int(TRAJECTORY_FPS_MAX)}).",
        )


def validate_trajectory_metadata(point_count: Any, duration_s: Any) -> None:
    """Validate the optional denormalised trajectory metadata columns before they
    hit Postgres — ``validate_trajectory`` guards the ``points``/``fps`` payload,
    but ``point_count``/``duration_s`` are otherwise stored verbatim from the body.

    ``point_count`` (an INTEGER/int4 column) must, when supplied, be a
    non-negative int within the int4 range (``bool`` excluded — it is an ``int``
    subclass and never a count); a larger value would overflow the column and
    raise an uncaught 500 on INSERT. ``duration_s`` (a DOUBLE PRECISION column)
    must, when supplied, be a finite non-negative number (``bool`` excluded;
    ``json.loads`` accepts bare ``NaN``/``Infinity`` so a finite-check is required
    — an ``Infinity`` would otherwise be stored verbatim). ``None`` is allowed for
    both (the route derives ``point_count`` from ``len(points)`` when omitted).
    German HTTP 400 on any violation. Defense-in-depth only — the ROS server is
    the semantic authority; the cloud gate guards type/range."""
    if point_count is not None:
        if isinstance(point_count, bool) or not isinstance(point_count, int):
            raise HTTPException(
                status_code=400, detail="Punktanzahl muss eine ganze Zahl sein."
            )
        if point_count < 0 or point_count > INT4_MAX:
            raise HTTPException(
                status_code=400,
                detail="Punktanzahl liegt außerhalb des gültigen Bereichs.",
            )
    if duration_s is not None:
        if isinstance(duration_s, bool) or not isinstance(duration_s, (int, float)):
            raise HTTPException(
                status_code=400, detail="Dauer muss eine Zahl sein."
            )
        if not math.isfinite(duration_s) or duration_s < 0:
            raise HTTPException(
                status_code=400,
                detail="Dauer muss eine endliche, nicht-negative Zahl sein.",
            )
