FROM nvidia/cuda:12.8.0-runtime-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive

# System dependencies
RUN apt-get update && apt-get install -y \
    python3 python3-pip python3-venv git ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install LeRobot and dependencies
RUN pip3 install --no-cache-dir --break-system-packages \
    "lerobot[pi0]" \
    runpod \
    huggingface_hub \
    supabase

COPY handler.py .

CMD ["python3", "handler.py"]
