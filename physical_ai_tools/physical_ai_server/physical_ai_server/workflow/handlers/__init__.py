"""Block handler dispatch tables.

Two tables: ``STATEMENT_HANDLERS`` for blocks that DO things (motion,
destination pin, output) and ``VALUE_EVALUATORS`` for blocks that
RETURN things (perception detect/count, math, logic, variables_get).

The interpreter picks the appropriate table based on the block's
position in the tree (top-level / next chain / DO0 chain → statement;
inputs / value-block slot → value).
"""

from physical_ai_server.workflow.handlers import motion as motion_handlers
from physical_ai_server.workflow.handlers import perception_blocks as perception_handlers
from physical_ai_server.workflow.handlers import destinations as destination_handlers
from physical_ai_server.workflow.handlers import output as output_handlers
from physical_ai_server.workflow.handlers import counters as counter_handlers
from physical_ai_server.workflow.handlers import trajectory as trajectory_handlers


STATEMENT_HANDLERS: dict[str, callable] = {
    # Motion
    'edubotics_home': motion_handlers.home,
    'edubotics_open_gripper': motion_handlers.open_gripper,
    'edubotics_close_gripper': motion_handlers.close_gripper,
    'edubotics_move_to': motion_handlers.move_to,
    'edubotics_pickup': motion_handlers.pickup,
    'edubotics_drop_at': motion_handlers.drop_at,
    'edubotics_wait_seconds': motion_handlers.wait_seconds,
    # Replay a recorded hand-guided trajectory (Batch 2b). Reads
    # ctx.trajectories[name] (CONTRACT C sibling of the workflow_json) and
    # re-segments through the velocity floor — see handlers/trajectory.py.
    'edubotics_replay_trajectory': trajectory_handlers.replay_trajectory,
    # Grasp-split motion primitives (Phase 1) — the one-block grasp decomposed
    # into composable steps acting on a Greifziel (a Detection from find_object).
    'edubotics_move_above': motion_handlers.move_above,
    'edubotics_descend_to': motion_handlers.descend_to,
    'edubotics_close_on_object': motion_handlers.close_on_object,
    'edubotics_lift': motion_handlers.lift,
    # Named-object grasp (Roboter Studio AprilTag grasping)
    'edubotics_grasp_object': perception_handlers.grasp_object,
    # Grasp-split claim — marks a Greifziel done so a "Solange sichtbar" loop
    # using the split path still makes progress (the split blocks don't auto-claim).
    'edubotics_mark_done': perception_handlers.mark_done,
    # Destinations
    'edubotics_destination_pin': destination_handlers.destination_pin,
    'edubotics_destination_current': destination_handlers.destination_current,
    # Output
    'edubotics_log': output_handlers.log,
    'edubotics_play_sound': output_handlers.play_sound,
    'edubotics_speak_de': output_handlers.speak_de,
    'edubotics_play_tone': output_handlers.play_tone,
    'edubotics_toast': output_handlers.toast,
    # Counters (named per-run integer counters). The get block is a VALUE
    # evaluator below; the when_counter_gt hat is interpreter-native
    # (HAT_BLOCK_TYPES + WorkflowManager._wait_for_hat_trigger).
    'edubotics_counter_reset': counter_handlers.reset,
    'edubotics_counter_add': counter_handlers.add,
}


VALUE_EVALUATORS: dict[str, callable] = {
    # WS3: value-output reference to a pinned destination (fills move_to/drop_at).
    'edubotics_destination_ref': destination_handlers.destination_ref,
    # Named-object value blocks (Roboter Studio AprilTag grasping). The legacy
    # colour / COCO-object / raw-marker / open-vocabulary detection blocks were
    # removed in the named-object cleanup (P4) — AprilTag named-object detection
    # is the only perception workflow now.
    'edubotics_see_object': perception_handlers.see_object,
    'edubotics_count_object': perception_handlers.count_object,
    'edubotics_wait_until_object_seen': perception_handlers.wait_until_object_seen,
    # Poll until the gripper holds an object (or a German timeout). Mirrors
    # grasp_held's no-silent-False contract (raises on missing joint readback).
    'edubotics_wait_until_held': perception_handlers.wait_until_held,
    # Grasp-split value blocks (Phase 1): find a graspable object (→ Greifziel),
    # read its position, and check whether the gripper is holding something.
    'edubotics_find_object': perception_handlers.find_object,
    'edubotics_object_position': perception_handlers.object_position,
    'edubotics_grasp_held': perception_handlers.grasp_held,
    # Counter value block.
    'edubotics_counter_get': counter_handlers.get,
}


# Legacy alias kept for backwards compat with code that imports HANDLERS.
HANDLERS = STATEMENT_HANDLERS
