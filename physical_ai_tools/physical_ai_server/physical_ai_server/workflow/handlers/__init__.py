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


STATEMENT_HANDLERS: dict[str, callable] = {
    # Motion
    'edubotics_home': motion_handlers.home,
    'edubotics_open_gripper': motion_handlers.open_gripper,
    'edubotics_close_gripper': motion_handlers.close_gripper,
    'edubotics_move_to': motion_handlers.move_to,
    'edubotics_pickup': motion_handlers.pickup,
    'edubotics_drop_at': motion_handlers.drop_at,
    'edubotics_wait_seconds': motion_handlers.wait_seconds,
    # Destinations
    'edubotics_destination_pin': destination_handlers.destination_pin,
    'edubotics_destination_current': destination_handlers.destination_current,
    # Output
    'edubotics_log': output_handlers.log,
    'edubotics_play_sound': output_handlers.play_sound,
    'edubotics_speak_de': output_handlers.speak_de,
    'edubotics_play_tone': output_handlers.play_tone,
}


VALUE_EVALUATORS: dict[str, callable] = {
    'edubotics_detect_color': perception_handlers.detect_color,
    'edubotics_detect_object': perception_handlers.detect_object,
    'edubotics_detect_marker': perception_handlers.detect_marker,
    'edubotics_count_color': perception_handlers.count_color,
    'edubotics_count_objects_class': perception_handlers.count_objects_class,
    'edubotics_wait_until_color': perception_handlers.wait_until_color,
    'edubotics_wait_until_object': perception_handlers.wait_until_object,
    'edubotics_wait_until_marker': perception_handlers.wait_until_marker,
    'edubotics_detect_open_vocab': perception_handlers.detect_open_vocab,
}


# Legacy alias kept for backwards compat with code that imports HANDLERS.
HANDLERS = STATEMENT_HANDLERS
