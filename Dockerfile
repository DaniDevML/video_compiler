# VidCompiler — portable image, runnable on any machine with Docker.
#
# Build:  docker build -t vidcompiler .
# Run:    docker run --rm -p 5000:5000 -v vidcompiler-data:/data vidcompiler
#
# Then open http://localhost:5000 on the host.
#
# On hardware acceleration: the image deliberately does not depend on CUDA or
# NVENC. Those need the host's driver and the NVIDIA container runtime, which
# is exactly the assumption that stops an image being shareable. The encoder
# probe falls back to libx264 on its own, so the container works everywhere and
# uses the GPU only where one is actually exposed:
#
#   docker run --rm --gpus all -p 5000:5000 -v vidcompiler-data:/data vidcompiler
#
# Software encoding is slower than NVENC but produces an identical bitstream as
# far as the codec is concerned -- the payload is unaffected.

# ---------------------------------------------------------------------------
# Stage 1: compile the native pixel / Reed-Solomon library
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS native

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY native/ ./native/

# Portable codegen: -march=native would tie the image to whichever CPU built
# it, which defeats the point of shipping an image.
ENV VIDCOMPILER_PORTABLE=1
RUN python native/build.py && test -f native/frame_ops.so

# ---------------------------------------------------------------------------
# Stage 2: the runtime image
# ---------------------------------------------------------------------------
FROM python:3.11-slim

# ffmpeg from the distribution: it carries libx264, which is what this image
# encodes with in the absence of a GPU.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 app

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt waitress

COPY --chown=app:app . .
COPY --from=native /src/native/frame_ops.so ./native/frame_ops.so

# /data holds everything that must outlive the container: OAuth credentials,
# the saved token, and scratch space. A job needs roughly 7x the payload here,
# so mount a volume with room rather than relying on the container filesystem.
RUN mkdir -p /data && chown app:app /data
VOLUME ["/data"]

ENV VIDCOMPILER_CONTAINER=1 \
    VIDCOMPILER_DATA=/data \
    VIDCOMPILER_SCRATCH=/data/.scratch \
    NUMBA_CACHE_DIR=/data/.numba \
    PYTHONUNBUFFERED=1

USER app
EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD python -c "import urllib.request,sys; \
        sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5000/', timeout=4).status==200 else 1)"

CMD ["python", "launcher.py"]
