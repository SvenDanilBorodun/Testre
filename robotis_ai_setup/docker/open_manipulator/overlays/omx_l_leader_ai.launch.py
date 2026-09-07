#!/usr/bin/env python3
#
# Copyright 2024 ROBOTIS CO., LTD.
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
# Author: Wonho Yun, Sungho Woo, Woojin Wie, Junha Cha

import os

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.actions import ExecuteProcess
from launch.actions import GroupAction
from launch.actions import RegisterEventHandler
from launch.conditions import UnlessCondition
from launch.event_handlers import OnProcessExit
from launch.substitutions import Command
from launch.substitutions import FindExecutable
from launch.substitutions import LaunchConfiguration
from launch.substitutions import PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.actions import PushRosNamespace
from launch_ros.substitutions import FindPackageShare


def generate_launch_description():
    # Declare launch arguments
    declared_arguments = [
        DeclareLaunchArgument(
            'prefix',
            default_value='""',
            description='Prefix of the joint and link names',
        ),
        DeclareLaunchArgument(
            'use_sim',
            default_value='false',
            description='Start robot in Gazebo simulation.',
        ),
        DeclareLaunchArgument(
            'use_mock_hardware',
            default_value='false',
            description='Use mock hardware mirroring command.',
        ),
        DeclareLaunchArgument(
            'mock_sensor_commands',
            default_value='false',
            description='Enable mock sensor commands.',
        ),
        DeclareLaunchArgument(
            'port_name',
            default_value='/dev/ttyACM2',
            description='Port name for hardware connection.',
        ),
        DeclareLaunchArgument(
            'ros2_control_type',
            default_value='omx_l',
            description='Type of ros2_control',
        ),
    ]

    # Launch configurations
    prefix = LaunchConfiguration('prefix')
    use_sim = LaunchConfiguration('use_sim')
    use_mock_hardware = LaunchConfiguration('use_mock_hardware')
    mock_sensor_commands = LaunchConfiguration('mock_sensor_commands')
    port_name = LaunchConfiguration('port_name')
    ros2_control_type = LaunchConfiguration('ros2_control_type')

    # Generate URDF file using xacro
    urdf_file = Command([
        PathJoinSubstitution([FindExecutable(name='xacro')]),
        ' ',
        PathJoinSubstitution([
            FindPackageShare('open_manipulator_description'),
            'urdf',
            'omx_l',
            'omx_l.urdf.xacro',
        ]),
        ' ',
        'prefix:=',
        prefix,
        ' ',
        'use_sim:=',
        use_sim,
        ' ',
        'use_mock_hardware:=',
        use_mock_hardware,
        ' ',
        'mock_sensor_commands:=',
        mock_sensor_commands,
        ' ',
        'port_name:=',
        port_name,
        ' ',
        'ros2_control_type:=',
        ros2_control_type,
    ])

    # Paths for configuration files
    controller_manager_config = PathJoinSubstitution([
        FindPackageShare('open_manipulator_bringup'),
        'config',
        'omx_l_leader_ai',
        'hardware_controller_manager.yaml',
    ])

    control_node = Node(
        package='controller_manager',
        executable='ros2_control_node',
        parameters=[{'robot_description': urdf_file}, controller_manager_config],
        output='both',
        condition=UnlessCondition(use_sim),
    )

    # Gravity compensation was intentionally removed — the leader arm runs
    # with no effort writer on joints 1-5, so it goes limp on boot. The
    # operator must support the arm by hand during teleop.
    #
    # TELEOP GATE (2026-09-07). `joint_trajectory_command_broadcaster` is what
    # republishes the leader's live pose onto /leader/joint_trajectory at
    # 100 Hz — i.e. it IS teleoperation, and while it is spawned the follower
    # mirrors the leader. Spawning it here meant teleop was live from container
    # boot: before a browser was open, before anyone had logged in, and with
    # the follower snapping onto whatever pose gravity had left the limp leader
    # in. So with EDUBOTICS_REQUIRE_ACTIVATION on (the default) it is LEFT OUT
    # of this spawner and started later by
    # /usr/local/bin/activation_agent.py::_start_teleop, after that agent has
    # homed the follower and glided it onto the leader pose.
    #
    # Read straight from the environment rather than a launch argument: the
    # entrypoint does not pass arguments to this launch file, and the same env
    # var is the single rollback for all four layers that honour the gate.
    # Leaving the controller out of the spawner means /leader/joint_trajectory
    # has NO PUBLISHER until activation — a structural gate, not a flag some
    # code path can forget to consult. The trigger_position_controller and its
    # 50 Hz -0.7 rad publish below STAY: they simulate the spring on the
    # leader's OWN trigger under a 300 mA cap and move no follower joint, and a
    # JointGroupPositionController left with no command is its own hazard.
    _require_activation = (
        os.environ.get('EDUBOTICS_REQUIRE_ACTIVATION', '1').strip() != '0')
    _boot_controllers = ['joint_state_broadcaster', 'trigger_position_controller']
    if not _require_activation:
        _boot_controllers.append('joint_trajectory_command_broadcaster')

    robot_controller_spawner = Node(
        package='controller_manager',
        executable='spawner',
        arguments=_boot_controllers,
        parameters=[{'robot_description': urdf_file}],
    )

    robot_state_publisher_node = Node(
        package='robot_state_publisher',
        executable='robot_state_publisher',
        parameters=[{'robot_description': urdf_file,
                     'use_sim_time': use_sim,
                     'frame_prefix': 'leader_'}],
        output='both',
    )

    # Execute process to publish position command
    position_command_process = ExecuteProcess(
        name='trigger_position_command',
        cmd=[
            'ros2', 'topic', 'pub',
            '-r', '50',
            '-t', '50',
            '-p', '50',
            '/leader/trigger_position_controller/commands',
            'std_msgs/msg/Float64MultiArray',
            'data: [-0.7]',
        ],
    )

    delay_position_command_after_controllers = RegisterEventHandler(
        event_handler=OnProcessExit(
            target_action=robot_controller_spawner,
            on_exit=[position_command_process],
        )
    )

    leader_with_namespace = GroupAction(
        actions=[
            PushRosNamespace('leader'),
            control_node,
            robot_controller_spawner,
            robot_state_publisher_node,
            delay_position_command_after_controllers,
        ]
    )

    return LaunchDescription(declared_arguments + [leader_with_namespace])
