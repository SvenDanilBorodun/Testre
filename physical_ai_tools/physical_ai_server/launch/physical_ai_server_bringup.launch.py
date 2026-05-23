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

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch_ros.actions import Node


def generate_launch_description():
    pkg_dir = get_package_share_directory('physical_ai_server')

    # Include physical_ai_server.launch.py
    physical_ai_server_launch = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(pkg_dir, 'launch', 'physical_ai_server.launch.py')
        )
    )

    # Rosbridge websocket node
    rosbridge_websocket_node = Node(
        package='rosbridge_server',
        executable='rosbridge_websocket',
        name='rosbridge_websocket',
        output='screen',
    )

    # Include rosbag_recorder service_bag_recorder node
    rosbag_recorder_node = Node(
        package='rosbag_recorder',
        executable='service_bag_recorder',
        name='service_bag_recorder',
        output='screen'
    )

    # web_video_server node
    # Bind 0.0.0.0 inside the container. Docker port-publish forwards
    # host packets to the container's eth0, not its loopback — a
    # container-side 127.0.0.1 bind makes the stream unreachable
    # (Aufnahme cells stay blank). LAN isolation is enforced by the
    # compose mapping `127.0.0.1:8080:8080`, which is the actual
    # defence-in-depth. Audit F36's original 127.0.0.1 bind was the
    # root cause of the blank-preview regression; fixed 2026-05-23.
    #
    # respawn=True: web_video_server has been observed to SIGSEGV on
    # malformed CompressedImage frames (e.g. mid-replug). Without
    # respawn the browser MJPEG preview stays blank until container
    # restart. 2 s delay throttles a runaway crash loop while still
    # recovering inside one human heartbeat.
    web_video_server_node = Node(
        package='web_video_server',
        executable='web_video_server',
        name='web_video_server',
        parameters=[{'address': '0.0.0.0', 'port': 8080}],
        output='screen',
        respawn=True,
        respawn_delay=2.0,
    )

    return LaunchDescription([
        physical_ai_server_launch,
        rosbridge_websocket_node,
        rosbag_recorder_node,
        web_video_server_node
    ])
