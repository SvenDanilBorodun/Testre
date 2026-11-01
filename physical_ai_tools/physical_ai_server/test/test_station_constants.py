"""Literal pins for the OMX camera station's chosen numbers (plan v6.13), one
assertion per constant, so a mutation of a shipped value fails the suite
(test_constant_pins' rule B-0)."""

from physical_ai_server.station import check, commission, commission_worker, state, store
from physical_ai_server.station import texts_de
from physical_ai_server.workflow import code_rpc


def test_state_pins():
    assert state.KEEPALIVE_LOST_S == 10.0
    assert state.TAKEOVER_AFTER_S == 4.0
    assert state.NATIVE_TIMEOUT_S == 30.0
    assert state.FRAME_GAP_S == 2.0
    assert state.ENDED_MEMORY == 16
    assert state.HEALTH_MIN_FRAMES == 10
    assert state.HEALTH_MIN_SPAN_S == 1.0
    assert state.HEALTH_MIN_RATE == 10.0

def test_commission_worker_pins():
    assert commission_worker.RETRY_DELAY_S == 2.0
    assert commission_worker.IDLE_RELOCK_S == 30.0
    assert commission_worker.RELOCK_ATTEMPTS == 3

def test_texts_de_pins():
    assert texts_de.STATION_PROTOCOL == 6

def test_store_pins():
    assert store.SCHEMA == 6
    assert store.EVIDENCE_KEEP == 3

def test_code_rpc_pins():
    assert code_rpc.STATION_TAG_ID_MIN == 100
    assert code_rpc.STATION_TAG_ID_MAX == 199

def test_check_pins():
    assert check.OK_RMS_MM == 3.0
    assert check.OK_MAX_MM == 6.0
    assert check.OK_CENTROID_MM == 3.0
    assert check.OK_ROTATION_DEG == 0.5
    assert check.FAR_MAX_MM == 25.0
    assert check.ROBOT_NEAR_DOT_MM == 12.0
    assert check.ROBOT_DXY_MM == 4.0
    assert check.ROBOT_DZ_MM == 3.0
    assert check.MAX_TILT_DEG == 12.0
    assert check.JOINTS_MAX_AGE_S == 0.5
    assert check.RESOLVE_SHIFT_MM == 25.0
    assert check.RESOLVE_ROT_DEG == 3.0
    assert check.MAT_GROW_M == 0.06

def test_commission_pins():
    assert commission.F0_PX == 1100.0
    assert commission.LENS_MIN_VIEWS == 25
    assert commission.LENS_MIN_CORNERS == 12
    assert commission.LENS_MIN_X_POSITIONS == 4
    assert commission.LENS_MIN_Y_POSITIONS == 3
    assert commission.LENS_MIN_CELLS == 10
    assert commission.LENS_AREA_RATIO == 1.5
    assert commission.LENS_TILT_SPREAD_DEG == 15.0
    assert commission.DUP_DISPLACEMENT_PX == 10.0
    assert commission.DUP_AREA == 0.05
    assert commission.DUP_NORMAL_DEG == 5.0
    assert commission.FIT_RMS_PX == 1.0
    assert commission.HELDOUT_RMS_PX == 1.5
    assert commission.HELDOUT_MAX_PX == 4.0
    assert commission.ROUNDTRIP_PX == 0.1
    assert commission.F_BAND == 0.15
    assert commission.FXFY_TOL == 0.02
    assert commission.C_TOL_PX == 80.0
    assert commission.BOARD_BURST == 8
    assert commission.BOARD_MIN_CORNERS == 20
    assert commission.BOARD_MAX_REPROJ_PX == 1.0
    assert commission.BOARD_MIN_PASS == 4
    assert commission.CAMERA_MIN_Z_M == 0.3
    assert commission.TAP_TILT_DEG == 12.0
    assert commission.WRONG_DOT_MM == 40.0
    assert commission.REG_RMS_MM == 2.0
    assert commission.REG_MAX_MM == 3.5
    assert commission.REG_SHIFT_MM == 8.0
    assert commission.REG_ROT_DEG == 1.5
    assert commission.CUBE_TOP_Z_M == 0.03
    assert commission.WORLD_RMS_MM == 3.0
    assert commission.WORLD_MAX_MM == 6.0
    assert commission.WORLD_POINTS == 20
    assert commission.ZONE_GROW_M == 0.01


def test_the_image_size():
    assert texts_de.STATION_IMAGE_SIZE == (1920, 1080)
