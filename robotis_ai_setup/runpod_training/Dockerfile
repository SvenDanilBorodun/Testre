FROM nvidia/cuda:12.8.0-devel-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive

# System dependencies
RUN apt-get update && apt-get install -y \
    python3 python3-pip python3-venv git ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install LeRobot from the SAME commit used by physical_ai_server (v0.2.0)
# This ensures dataset format, model format, and policy names match exactly.
# Commit: 989f3d05 from https://github.com/huggingface/lerobot
RUN pip3 install --no-cache-dir --break-system-packages \
    "lerobot[pi0] @ git+https://github.com/huggingface/lerobot.git@989f3d05ba47f872d75c587e76838e9cc574857a" \
    runpod \
    huggingface_hub \
    supabase

COPY handler.py .

CMD ["python3", "handler.py"]
