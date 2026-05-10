# Live sensor snapshot, emitted at ~5 Hz from the workflow runtime
# when a workflow is active (the React debugger panel subscribes).
# The fields are intentionally lightweight: this message is published
# alongside the regular WorkflowStatus topic and shouldn't add
# noticeable overhead during inference.
std_msgs/Header header
float32[] follower_joints              # rad, length matches the arm DoF
float32 gripper_opening                # rad, 0..0.8 typical
int32[] visible_apriltag_ids           # tag36h11 IDs in the most recent frame
int32[] color_counts                   # [rot, gruen, blau, gelb] in pixels
string[] visible_object_classes        # COCO labels active in the latest frame
