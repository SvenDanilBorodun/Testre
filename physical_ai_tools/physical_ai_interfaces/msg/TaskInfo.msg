string task_name
string task_type
string user_id
string[] task_instruction
string policy_path
uint8 fps
string[] tags
uint16 warmup_time_s    # [s]
uint16 episode_time_s   # [s]
uint16 reset_time_s     # [s]
uint16 num_episodes
bool push_to_hub
# Defaults to TRUE — fail safe to a PRIVATE HuggingFace repo. ROS 2 booleans
# default to false when a field carries no default, so an omitted private_mode
# used to publish classroom recordings — which can contain children's faces and
# audio — to a PUBLIC repo. React always sends true, which is what kept it
# latent; any other rosbridge client got public. Keep in lockstep with
# data_manager._upload_dataset's `private=True`.
bool private_mode true
bool use_optimized_save_mode
bool record_inference_mode
bool record_rosbag2
