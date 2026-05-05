# Live status of the running workflow. Published by WorkflowManager on
# /workflow/status. active_detections mirrors the most recent
# perception result so the editor can overlay bboxes on the camera
# feed.
#
# v1 (commit 8a6314f) used parallel geometry_msgs/Point[] +
# string[] active_detection_labels arrays, but Point has no slot for
# bbox width/height — the React side was hacking d.z as the width.
# Replaced with a typed Detection[] carrying (cx, cy, w, h, label,
# confidence). See audit §1.6.
string workflow_id
string current_block_id
string phase
string log_message
float32 progress
string error
Detection[] active_detections
