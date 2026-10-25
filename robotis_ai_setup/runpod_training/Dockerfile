# EduBotics managed-training image for RunPod pods (spec §4.8). No secret is baked in.
#
#   docker build --build-arg BUILD_COMMIT=<sha> -f robotis_ai_setup/runpod_training/Dockerfile -t <img> .
#
# Context: the repo root. Built and pushed only by .github/workflows/trainer-image.yml;
# the cloud API pins it by digest (RUNPOD_TRAINER_IMAGE). The dependency set is the ONE
# training lock (modal_training/training-lock.txt, CLAUDE.md Rule §5), installed with
# --no-deps exactly like the Modal app, and checked strictly at build time.

# ---- builder: compile the venv (evdev is an sdist with a C extension) ----
FROM python:3.12.14-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9 AS builder
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc g++ build-essential \
 && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH PIP_DISABLE_PIP_VERSION_CHECK=1
COPY robotis_ai_setup/modal_training/training-lock.txt /build/training-lock.txt
COPY robotis_ai_setup/modal_training/check_training_lock.py /build/check_training_lock.py
RUN python -m pip install --no-cache-dir --no-deps -r /build/training-lock.txt --index-url https://pypi.org/simple --extra-index-url https://download.pytorch.org/whl/cu126
RUN python /build/check_training_lock.py /build/training-lock.txt --strict
# ACT's backbone weights (ResNet18_Weights.IMAGENET1K_V1), so a pod never downloads them.
ENV TORCH_HOME=/opt/torch
RUN python -c "import torchvision; torchvision.models.ResNet18_Weights.IMAGENET1K_V1.get_state_dict(progress=False)" \
 && ls -l /opt/torch/hub/checkpoints

# ---- runtime ----
FROM python:3.12.14-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9
ARG BUILD_COMMIT=unknown
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /opt/torch /opt/torch
COPY robotis_ai_setup/modal_training/training_handler.py /app/training_handler.py
COPY robotis_ai_setup/modal_training/check_training_lock.py /app/check_training_lock.py
COPY robotis_ai_setup/modal_training/training-lock.txt /app/training-lock.txt
COPY robotis_ai_setup/runpod_training/entry.py /app/entry.py
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 TORCH_HOME=/opt/torch HF_HUB_DISABLE_TELEMETRY=1 PYTHONPATH=/app EDUBOTICS_BUILD=${BUILD_COMMIT}
RUN python /app/check_training_lock.py --strict
ENTRYPOINT ["python", "-u", "/app/entry.py"]
