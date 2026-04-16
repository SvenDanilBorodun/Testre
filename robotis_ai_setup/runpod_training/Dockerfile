FROM nvidia/cuda:12.1.1-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Runtime deps kept permanent: ffmpeg (pyav decode), git (pip git+https),
# ca-certs, python. Build-essential + python3-dev installed only for the pip
# compile step (evdev + a few others need gcc) then purged in the same layer
# so the final image keeps the runtime base's small footprint.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-pip git ffmpeg ca-certificates \
      build-essential python3-dev \
 && pip3 install --upgrade pip \
 && pip3 install \
      torch==2.4.1 torchvision==0.19.1 \
      --index-url https://download.pytorch.org/whl/cu121 \
 && pip3 install \
      "lerobot @ git+https://github.com/huggingface/lerobot.git@989f3d05ba47f872d75c587e76838e9cc574857a" \
      runpod huggingface_hub supabase \
 && apt-get purge -y --auto-remove build-essential python3-dev \
 && rm -rf /var/lib/apt/lists/* /root/.cache/pip /tmp/*

WORKDIR /app
COPY handler.py .

CMD ["python3", "handler.py"]
