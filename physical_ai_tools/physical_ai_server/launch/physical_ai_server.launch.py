#!/usr/bin/env python3
#
# Copyright 2025 ROBOTIS CO., LTD.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Author: Dongyun Kim

import glob
import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description():
    # Find package share directory for the physical_ai_server package
    pkg_dir = get_package_share_directory('physical_ai_server')

    config_dir = os.path.join(pkg_dir, 'config')
    config_files = glob.glob(os.path.join(config_dir, '*.yaml'))
    config_files.sort()

    # respawn=True: this node carries ~35 service callbacks + timers + the
    # camera/recording pipeline on a MultiThreadedExecutor; an unhandled
    # exception in any callback propagates out of spin() and the process exits
    # (main() only catches KeyboardInterrupt). Without respawn the node stayed
    # DEAD behind a still-running rosbridge (only web_video_server had respawn),
    # so the React app showed "Getrennt" with no recovery short of a full
    # environment restart. respawn restarts the node in place (rosbridge keeps
    # its socket, so the browser barely notices); the robot type is resolved
    # from EDUBOTICS_ROBOT_TYPE in __init__ (robot_profiles.resolve) and the
    # data pipeline boot-initializes with it, so a respawn self-heals from env
    # and the idle identity tick re-delivers robot_type + capabilities to the
    # browser within ~2-3 s — no client-side re-selection exists anymore.
    # 2 s delay throttles a crash loop (mirrors web_video_server's policy in
    # physical_ai_server_bringup.launch.py).
    physical_ai_server = Node(
        package='physical_ai_server',
        executable='physical_ai_server',
        name='physical_ai_server',
        output='screen',
        parameters=config_files,
        respawn=True,
        respawn_delay=2.0,
    )

    return LaunchDescription([
        physical_ai_server
    ])
