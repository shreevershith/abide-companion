# syntax=docker/dockerfile:1.6
#
# Abide Companion — single-service image.
#
# Build notes:
#  - Python 3.12 slim base keeps the image small.
#  - CPU-only torch is installed from PyTorch's CPU wheel index BEFORE
#    the rest of requirements.txt so we don't accidentally pull the
#    ~2GB CUDA build. silero-vad runs fine on CPU; this is a deliberate
#    latency-vs-size trade-off documented in DESIGN-NOTES.
#  - silero-vad model weights are cached at build time by invoking
#    load_silero_vad() during the build. First user turn does not pay
#    model-load cost at runtime.
#  - Application code is copied LAST so rebuilds after app edits are
#    fast (dependency layers are cached).

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

# Minimal system deps. libgomp1 is required by torch's OpenMP runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libgomp1 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 1. CPU-only torch + torchaudio first — pinned via the official CPU
#    index. Both must come from the same index: silero-vad depends on
#    torchaudio, and PyPI's torchaudio wheel links against CUDA
#    (libcudart.so.13), which is not present in this image. Installing
#    torchaudio here pre-empts the transitive resolve and keeps the
#    image CPU-only.
RUN pip install --index-url https://download.pytorch.org/whl/cpu \
        "torch>=2.0" "torchaudio>=2.0"

# 2. Everything else. torch is already satisfied and will be skipped.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# 3. Pre-cache silero-vad weights so startup is instant.
#    silero-vad >=4 bundles the ONNX model with the package, so this
#    call just exercises the loader and verifies the import path works.
RUN python -c "from silero_vad import load_silero_vad; load_silero_vad(); print('silero-vad weights cached')"

# 4. Copy application code (fast rebuild layer).
COPY app/ ./app/
COPY frontend/ ./frontend/

# 5. Run as a non-root user. Defense in depth: even though this is a
#    localhost prototype, running as root inside the container is a
#    footgun for any future deployment. `abide` owns the /app tree
#    so Python can read the code it needs.
RUN useradd --create-home --shell /usr/sbin/nologin --uid 10001 abide \
    && chown -R abide:abide /app
USER abide

EXPOSE 8000

# No --reload in production. Single worker — the app holds per-process
# in-memory state (conversation history, persistent httpx clients,
# silero-vad model) that must not be duplicated.
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--no-access-log"]
