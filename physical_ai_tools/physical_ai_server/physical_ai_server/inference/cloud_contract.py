#!/usr/bin/env python3
#
# Copyright 2026 EduBotics
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
"""Cloud-Inferenz: the robot's ROS surface, as constants (spec §5.2, frozen contract C4/C8).

Stdlib only. Read by the node, by the SPA's ``robotContract.js`` (lockstep test) and by the
Windows GUI's window-close hook (``gui/app/docker_manager.py`` CLOUD_STOP_SERVICE /
CLOUD_STOP_MARKER, lockstep test).

State payload on STATE_TOPIC (std_msgs/String, latched + every PUBLISH_PERIOD_S):
``json.dumps({k: … for k in STATE_KEYS}, ensure_ascii=False)`` in exactly that key order. Facts
only: ``rtt_ms_p50`` / ``gpu_ms_p50`` over the last 64 chunks, ``plan_age_ms`` since the last
adoption, ``hold_s`` while holding, ``ends_in_s`` = ``ready.limits.max_end_in_s`` minus the time since
``ready``, ``stale_camera`` a camera name or null, ``end_text_de`` the German end sentence of the last
run (null while running or idle). No token ever appears in it.
"""

START_SERVICE = '/edubotics/cloud_inference/start'           # physical_ai_interfaces/srv/StartCloudInference
STOP_SERVICE = '/edubotics/cloud_inference/stop'             # std_srvs/srv/Trigger
STATE_TOPIC = '/edubotics/cloud_inference_state'              # std_msgs/String JSON, latched + 1 Hz
STATE_SCHEMA_VERSION = 1
STATE_KEYS = ('v', 'seq', 'active', 'phase', 'rtt_ms_p50', 'plan_age_ms', 'gpu_ms_p50', 'hold_s', 'ends_in_s',
              'stale_camera', 'end_text_de')                    # O7': no transport / fallback keys
PHASES = ('idle', 'loading', 'running', 'holding', 'ended')
PUBLISH_PERIOD_S = 1.0
STATUS_MIN_PERIOD_S = 0.5                                     # /task/status ≤ 2 Hz during a cloud run (D18)
ACTIVE_MARKER_PATH = '/tmp/edubotics-cloud-inference.active'  # exists while a cloud run is active (D20)
