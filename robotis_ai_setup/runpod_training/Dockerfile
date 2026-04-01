FROM nvidia/cuda:12.1.1-devel-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive

# System dependencies
RUN apt-get update && apt-get install -y \
    python3 python3-pip python3-venv git ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Upgrade pip to fix metadata parsing for LeRobot
RUN pip3 install --no-cache-dir --upgrade pip

WORKDIR /app

# Install LeRobot at the same commit used by the ROBOTIS base image
# (ROBOTIS-GIT/physical_ai_tools jazzy branch pins this as a submodule)
RUN pip3 install --no-cache-dir \
    "lerobot[pi0] @ git+https://github.com/huggingface/lerobot.git@989f3d05ba47f872d75c587e76838e9cc574857a" \
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
