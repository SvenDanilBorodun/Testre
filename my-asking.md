So previously to set everything up students had to follow this tutorial: Setup Guide — ROS 2 (Physical AI Tools)
Overview
This guide shows how to set up and operate OMX using Physical AI Tools (Web UI). Follow the steps to prepare repositories, configure Docker, and run the teleoperation node.

INFO

System Requirements
Recommended OS	Ubuntu
Hardware requirement	NVIDIA GPU (CUDA-capable)
Software Setup
Prerequisites
Operating System: Any Linux distribution

The container runs Ubuntu 24.04 + ROS 2 Jazzy
The Host OS version does not need to match.
Docker Engine

Install using the official Docker guide

After installation:


sudo usermod -aG docker $USER
sudo systemctl enable docker
docker run hello-world
Git


sudo apt install git
NVIDIA Container Toolkit

Follow the official installation guide
Required steps:

Configure the production repository
Install nvidia-container-toolkit
Configure Docker runtime using nvidia-ctk
Restart Docker daemon
For detailed configuration, see the Docker configuration guide

Docker Volume Configuration
The Docker container uses volume mappings for hardware access, development, and data persistence:


volumes:
  # Hardware and system access
  - /dev:/dev
  - /tmp/.X11-unix:/tmp/.X11-unix:rw
  - /tmp/.docker.xauth:/tmp/.docker.xauth:rw

  # Development and data directories
  - ./workspace:/workspace
  - ../:/root/ros2_ws/src/open_manipulator/
TIP

Store your development code in /workspace to preserve your codes.

Set up Open Manipulator Docker Container
1. Start the Docker Container:
Clone the repository:

USER PC


git clone https://github.com/ROBOTIS-GIT/open_manipulator
Start the Open Manipulator container with the following command:


cd open_manipulator/docker && ./container.sh start
2. Set up launch file port
Enter the Open Manipulator Docker container:

USER PC


./container.sh enter
INFO

First, connect only the 'Leader' USB to the port, then check and copy the OpenRB serial ID.

USER PC or USER PC 🐋 OPEN MANIPULATOR


ls -al /dev/serial/by-id/
Serial device by-id listing example
As shown in the image below, paste the serial ID you noted above into the port name parameter for the [leader] then save.

USER PC 🐋 OPEN MANIPULATOR


sudo nano ~/ros2_ws/src/open_manipulator/open_manipulator_bringup/launch/omx_l_leader_ai.launch.py
# omx_l_leader_ai.launch.py
DeclareLaunchArgument(
    'port_name',
    default_value='/dev/serial/by-id/{your_leader_serial_id}',
    description='Port name for hardware connection.',
)
INFO

Second, connect only the 'Follower' USB to the port, then check and copy the OpenRB serial ID.

USER PC or USER PC 🐋 OPEN MANIPULATOR


ls -al /dev/serial/by-id/
Serial device by-id listing example
As shown in the image below, paste the serial ID you noted above into the port name parameter for the [follower], then save.

USER PC 🐋 OPEN MANIPULATOR


sudo nano ~/ros2_ws/src/open_manipulator/open_manipulator_bringup/launch/omx_f_follower_ai.launch.py
# omx_f_follower_ai.launch.py
DeclareLaunchArgument(
    'port_name',
    default_value='/dev/serial/by-id/{your_follower_serial_id}',
    description='Port name for hardware connection.',
)
INFO

Ultimately, it will be changed as shown below.

Serial device by-id listing example
🎉 Open Manipulator Container Setup Complete!

Please exit the Docker container and return to your host terminal for the next steps.

Set up Physical AI Tools Docker Container
1. Start the Docker container
Clone the repository along with all required submodules:

USER PC


git clone --recurse-submodules https://github.com/ROBOTIS-GIT/physical_ai_tools.git
Start the Physical AI Tools Docker container with the following command:


cd physical_ai_tools/docker && ./container.sh start
2. Configure camera topics
If you are using more than one camera or want to use a custom camera, list the available camera topics and choose the one you want to use:

Enter the Physical AI Tools Docker container:

USER PC


./container.sh enter
list the available topics to find your camera stream:

USER PC


ros2 topic list
And open the configuration file and update it as described below:

USER PC 🐋 PHYSICAL AI TOOLS


sudo nano ~/ros2_ws/src/physical_ai_tools/physical_ai_server/config/omx_f_config.yaml
Then update the fields outlined in red in the UI to point to your desired camera topic.

Configure camera topic in the UI
INFO

Note: The topic you set must always end with compressed
(for example, camera1/image_raw/compressed).

🎉 Physical AI Tools Container Setup Complete!

Click the button below to start Imitation Learning. Installing the NVIDIA Container Toolkit
Installation
Prerequisites
Read this section about platform support.

Install the NVIDIA GPU driver for your Linux distribution. NVIDIA recommends installing the driver by using the package manager for your distribution. For information about installing the driver with a package manager, refer to the NVIDIA Driver Installation Quickstart Guide. Alternatively, you can install the driver by downloading a .run installer.

Note

There is a known issue on systems where systemd cgroup drivers are used that cause containers to lose access to requested GPUs when systemctl daemon reload is run. Refer to the troubleshooting documentation for more information.

With apt: Ubuntu, Debian
Note

These instructions should work for any Debian-derived distribution.

Install the prerequisites for the instructions below:

sudo apt-get update && sudo apt-get install -y --no-install-recommends \
   ca-certificates \
   curl \
   gnupg2
Configure the production repository:

curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg \
  && curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
    sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
    sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
Optionally, configure the repository to use experimental packages:

sudo sed -i -e '/experimental/ s/^#//g' /etc/apt/sources.list.d/nvidia-container-toolkit.list
Update the packages list from the repository:

sudo apt-get update
Install the NVIDIA Container Toolkit packages:

export NVIDIA_CONTAINER_TOOLKIT_VERSION=1.19.0-1
  sudo apt-get install -y \
      nvidia-container-toolkit=${NVIDIA_CONTAINER_TOOLKIT_VERSION} \
      nvidia-container-toolkit-base=${NVIDIA_CONTAINER_TOOLKIT_VERSION} \
      libnvidia-container-tools=${NVIDIA_CONTAINER_TOOLKIT_VERSION} \
      libnvidia-container1=${NVIDIA_CONTAINER_TOOLKIT_VERSION}
With dnf: RHEL/CentOS, Fedora, Amazon Linux
Note

These instructions should work for many RPM-based distributions.

Install the prerequisites for the instructions below:

sudo dnf install -y \
   curl
Configure the production repository:

curl -s -L https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo | \
  sudo tee /etc/yum.repos.d/nvidia-container-toolkit.repo
Optionally, configure the repository to use experimental packages:

sudo dnf config-manager --enable nvidia-container-toolkit-experimental
Install the NVIDIA Container Toolkit packages:

export NVIDIA_CONTAINER_TOOLKIT_VERSION=1.19.0-1
  sudo dnf install -y \
      nvidia-container-toolkit-${NVIDIA_CONTAINER_TOOLKIT_VERSION} \
      nvidia-container-toolkit-base-${NVIDIA_CONTAINER_TOOLKIT_VERSION} \
      libnvidia-container-tools-${NVIDIA_CONTAINER_TOOLKIT_VERSION} \
      libnvidia-container1-${NVIDIA_CONTAINER_TOOLKIT_VERSION}
With zypper: OpenSUSE, SLE
Configure the production repository:

sudo zypper ar https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo
Optionally, configure the repository to use experimental packages:

sudo zypper modifyrepo --enable nvidia-container-toolkit-experimental
Install the NVIDIA Container Toolkit packages:

export NVIDIA_CONTAINER_TOOLKIT_VERSION=1.19.0-1
   sudo zypper --gpg-auto-import-keys install -y \
      nvidia-container-toolkit-${NVIDIA_CONTAINER_TOOLKIT_VERSION} \
      nvidia-container-toolkit-base-${NVIDIA_CONTAINER_TOOLKIT_VERSION} \
      libnvidia-container-tools-${NVIDIA_CONTAINER_TOOLKIT_VERSION} \
      libnvidia-container1-${NVIDIA_CONTAINER_TOOLKIT_VERSION}
Configuration
Prerequisites
You installed a supported container engine (Docker, Containerd, CRI-O, Podman).

You installed the NVIDIA Container Toolkit.

Configuring Docker
Configure the container runtime by using the nvidia-ctk command:

sudo nvidia-ctk runtime configure --runtime=docker
The nvidia-ctk command modifies the /etc/docker/daemon.json file on the host. The file is updated so that Docker can use the NVIDIA Container Runtime.

Restart the Docker daemon:

sudo systemctl restart docker
Rootless mode
To configure the container runtime for Docker running in Rootless mode, follow these steps:

Configure the container runtime by using the nvidia-ctk command:

nvidia-ctk runtime configure --runtime=docker --config=$HOME/.config/docker/daemon.json
Restart the Rootless Docker daemon:

systemctl --user restart docker
Configure /etc/nvidia-container-runtime/config.toml by using the sudo nvidia-ctk command:

sudo nvidia-ctk config --set nvidia-container-cli.no-cgroups --in-place
Configuring containerd (for Kubernetes)
Configure the container runtime by using the nvidia-ctk command:

sudo nvidia-ctk runtime configure --runtime=containerd
By default, the nvidia-ctk command creates a /etc/containerd/conf.d/99-nvidia.toml drop-in config file and modifies (or creates) the /etc/containerd/config.toml file to ensure that the imports config option is updated accordingly. The drop-in file ensures that containerd can use the NVIDIA Container Runtime.

Restart containerd:

sudo systemctl restart containerd
Configuring containerd (for nerdctl)
No additional configuration is needed. You can just run nerdctl run --gpus=all, with root or without root. You do not need to run the nvidia-ctk command mentioned above for Kubernetes.

Refer to the nerdctl documentation for more information.

Configuring CRI-O
Configure the container runtime by using the nvidia-ctk command:

sudo nvidia-ctk runtime configure --runtime=crio
By default, the nvidia-ctk command creates a /etc/crio/conf.d/99-nvidia.toml drop-in config file. The drop-in file ensures that CRI-O can use the NVIDIA Container Runtime.

Restart the CRI-O daemon:

sudo systemctl restart crio                                                                                                                                                                                          The pictures are from the tutorial. Now what has to be changed is to: Step 1: One-Time Installation
The student downloads Robotis_AI_Setup.exe and runs it. It does everything needed: Software Setup
Prerequisites
Operating System: Any Linux distribution

The container runs Ubuntu 24.04 + ROS 2 Jazzy
The Host OS version does not need to match.
Docker Engine

Install using the official Docker guide

After installation:


sudo usermod -aG docker $USER
sudo systemctl enable docker
docker run hello-world
Git


sudo apt install git
NVIDIA Container Toolkit

Follow the official installation guide
Required steps:

Configure the production repository
Install nvidia-container-toolkit
Configure Docker runtime using nvidia-ctk
Restart Docker daemon
For detailed configuration, see the Docker configuration guide

Docker Volume Configuration
The Docker container uses volume mappings for hardware access, development, and data persistence:


volumes:
  # Hardware and system access
  - /dev:/dev
  - /tmp/.X11-unix:/tmp/.X11-unix:rw
  - /tmp/.docker.xauth:/tmp/.docker.xauth:rw

  # Development and data directories
  - ./workspace:/workspace
  - ../:/root/ros2_ws/src/open_manipulator/
TIP

Store your development code in /workspace to preserve your codes. BUT STUDENTS WILL ONLY HAVE WINDOWS PCs!!!
A progress bar shows that showa what is beeing installed and configured . A desktop icon named "Launch Robotis AI" appears. Step 2: Hardware Setup (The GUI)
The student double-clicks the desktop icon. The custom GUI opens.
Step A (Leader): The GUI says: "Please plug in the LEADER arm via USB." The student plugs it in and clicks Scan. The GUI finds the USB device and locks it in.
Step B (Follower): The GUI says: "Please plug in the FOLLOWER arm." The student plugs it in and clicks Scan.
Step C (Camera): The GUI shows a dropdown of available webcams. The student selects their camera (e.g., Logitech C920).                                                                                                                                                       Step 3: Launch
The student clicks the "Start AI Environment" button.
A loading screen appears. The first time, it downloads the Docker image. On subsequent runs, it takes 3 seconds.
Automatically, the student's default web browser pops open to http://localhost:8080 (the Physical AI Web UI), fully connected to the robot arms and camera. They are ready to record Imitation Learning data!                                           Here how it might look like what is happening in the back: Phase A: Windows GUI prepares the environment
USB Passthrough: The Python GUI runs hidden PowerShell commands (usbipd bind and usbipd attach --wsl). This forces Windows to unhook the robot USBs and webcams and pass them directly into the Linux (WSL2) backend.
Path Discovery: The GUI asks WSL2 to list the connected devices and retrieves the exact hardware IDs (e.g., /dev/serial/by-id/usb-ROBOTIS_OpenRB-150_Leader123).
Generating the .env file: The GUI dynamically writes a text file called .env in the background:
code
Env
LEADER_PORT=/dev/serial/by-id/usb-ROBOTIS_OpenRB-150_Leader123
FOLLOWER_PORT=/dev/serial/by-id/usb-ROBOTIS_OpenRB-150_Follower456
Generating the YAML Config: Instead of editing the YAML file inside the container, the GUI creates a fresh omx_student_config.yaml file right on the Windows desktop, writing the exact camera topic the student selected.
Phase B: Docker Compose takes over
The GUI runs docker compose up -d. The docker-compose.yml file is designed to spin up two containers from your single Docker image:
Container 1 (Hardware Controller): Reads the .env file.
Container 2 (AI Server): Mounts the omx_student_config.yaml file over the default one using a Docker Volume (- ./omx_student_config.yaml:/workspace/physical_ai_server/config/omx_config.yaml).
Phase C: The Bulletproof entrypoint.sh executes
Inside Container 1, the entrypoint.sh wakes up.
Sources ROS 2: It runs source /opt/ros/jazzy/setup.bash and sources the Robotis workspace.
Hardware Validation: It physically checks if the USBs exist. If a student accidentally unhooked a cable, the script safely stops and prints: "ERROR: Leader arm not detected. Please check cables." instead of letting ROS crash wildly.
Native ROS 2 Launch (The Magic): It executes the final ROS command, passing the variables dynamically as ROS arguments:
code
Bash
ros2 launch open_manipulator_bringup omx_l_leader_ai.launch.py port_name:=$LEADER_PORT
Because we use ROS arguments, we do not need to edit ROBOTIS's python code. If Robotis updates their code tomorrow, this will still work perfectly.
Phase D: Communication and UI
The hardware container brings up the ROS 2 nodes for the OpenManipulators.
The AI container reads the securely mounted omx_student_config.yaml and starts the Web UI server.
Since both containers share the same Docker bridge network, they communicate seamlessly via ROS 2 DDS.
The Windows GUI detects the web server is alive and triggers webbrowser.open('http://localhost:8080'). As you probably guess i want a Docker Image where the two containers. Dive very deep into everything. Dont miss anything important. Dive very deep and create a plan on how to do everything i described. IM NOT A PROFI SO DONT RELY ON MY METHODS. IT JUST WAS WHAT I THINK WOULD WORK. FIND THE VERY BEST WAY FOR EACH ASPECT. THE PRIORITY IS THATH EVERYTHING HAS TO BE VERY STABLE WITH NO ISSUES AND VERY USERFRIENDLY. Here are the two containers from the original tutorial: @open_manipulator/ @physical_ai_tools/