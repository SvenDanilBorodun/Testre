# Live status of the running workflow. Published by WorkflowManager on
# /workflow/status. active_detections + active_detection_labels mirror
# the most recent perception result so the editor can overlay bboxes.
string workflow_id
string current_block_id
string phase
string log_message
float32 progress
string error
geometry_msgs/Point[] active_detections
string[] active_detection_labels
