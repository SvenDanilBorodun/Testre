FROM nvidia/cuda:12.1.1-devel-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive

# System dependencies
RUN apt-get update && apt-get install -y \
    python3 python3-pip python3-venv git ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Upgrade pip to fix metadata parsing for LeRobot
RUN pip3 install --no-cache-dir --upgrade pip

WORKDIR /app

# Copy the same LeRobot fork used by physical_ai_server (v0.2.0 with ROBOTIS patches)
# Build script must copy physical_ai_tools/lerobot/ into runpod_training/lerobot/ before build
COPY lerobot/ /tmp/lerobot/

# Remove torch/torchvision from LeRobot deps (overridden below with CUDA 12.1 builds)
RUN sed -i '/"torch[^"]*",/d' /tmp/lerobot/pyproject.toml && \
    sed -i '/"torchvision[^"]*",/d' /tmp/lerobot/pyproject.toml

# Install LeRobot from local source + runtime deps
RUN pip3 install --no-cache-dir "/tmp/lerobot[pi0]" \
    runpod \
    huggingface_hub \
    supabase

# Override PyTorch with CUDA 12.1 build (must come AFTER lerobot install)
# lerobot pulls torch+cu130 from PyPI which needs CUDA 13 libs not in base image
RUN pip3 install --no-cache-dir --force-reinstall \
    torch torchvision --index-url https://download.pytorch.org/whl/cu121

# Remove torchcodec (compiled for cu130, broken with cu121 torch) — pyav fallback used
RUN pip3 uninstall -y torchcodec || true

COPY handler.py .

CMD ["python3", "handler.py"]
