########################################
# Constants
########################################
# phase
uint8 READY = 0
uint8 WARMING_UP = 1
uint8 RESETTING = 2
uint8 RECORDING = 3
uint8 SAVING = 4
uint8 STOPPED = 5
uint8 INFERENCING = 6
uint8 COLLISION = 7             # follower forced against an object during teleop; stopped in place (modal step 1)
uint8 COLLISION_HOMING = 8      # safe-home glide in progress (HOME_FOLLOWER accepted)
uint8 COLLISION_HOMED = 9       # follower verified at the home pose (modal step 2)
uint8 INFERENCE_LOADING = 10    # policy load + processor build after START_INFERENCE (eager load; 10-30 s)

TaskInfo task_info
string robot_type
uint8 phase                     # (0: READY, 1: WARMING_UP, 2: RESETTING 3: RECORDING, 4: SAVING, 5: STOPPED, 6: INFERENCING, 7: COLLISION, 8: COLLISION_HOMING, 9: COLLISION_HOMED, 10: INFERENCE_LOADING)
uint16 total_time               # [s]
uint16 proceed_time             # [s]
uint16 current_episode_number
uint16 current_scenario_number
string current_task_instruction
float32 encoding_progress       # [%]
float32 used_storage_size       # [GB]
float32 total_storage_size      # [GB]
float32 used_cpu                # [%]
float32 used_ram_size           # [GB]
float32 total_ram_size          # [GB]
string error
float32[] joint_dist_to_home    # per-arm-joint |pos - safe_home| (rad) during the collision
                                # safe-home glide (joint1..joint5 order); empty otherwise.
                                # Publishers set it getattr-safe: pre-rebuild compiled
                                # interfaces lack the field and must keep publishing.
string robot_profile            # ArmProfile id resolved from EDUBOTICS_ROBOT_TYPE
                                # (e.g. "omx_full", "omx_follower"); empty on
                                # pre-rebuild compiled interfaces.
string capabilities_json        # compact JSON capability manifest of the resolved
                                # profile (recordable/editable/trainable/inferable/
                                # roboter_studio/has_leader). Publishers set both
                                # fields hasattr-guarded: pre-rebuild compiled
                                # interfaces lack them and must keep publishing.
