# agent-notes-embed-server image: the warm embedding service for every agent-notes host.
# Build context: a source archive holding this file and src/agent_notes (see build-image.ps1).
FROM python:3.13-slim@sha256:2d44738ebf4ce5f63b9d9ea871e9c7cfdc0426f7b0c97cac915fd88ded1d86bb

ARG VERSION=dev
ARG REVISION=unknown
LABEL org.opencontainers.image.source="https://github.com/hraedon/agent-notes" \
      org.opencontainers.image.version="$VERSION" \
      org.opencontainers.image.revision="$REVISION"

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/opt/hf

# CPU-only torch keeps CUDA (several GB) out of the image. Versions match the
# hosts' in-process path so a fallback encode gives the same vectors.
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch==2.15.0 \
 && pip install sentence-transformers==6.2.0 transformers==5.22.1 einops==0.8.2 \
        numpy==2.5.4 platformdirs

# Bake the model at a pinned revision so pods start offline and every replica
# serves the same weights. snapshot_download by commit doesn't write refs/main,
# which the offline loader resolves "nomic-ai/nomic-embed-text-v1.5" through.
ARG MODEL=nomic-ai/nomic-embed-text-v1.5
ARG MODEL_REVISION=e9b6763023c676ca8431644204f50c2b100d9aab
RUN python -c "from huggingface_hub import snapshot_download as d; d('$MODEL', revision='$MODEL_REVISION', allow_patterns=['*.json', 'model.safetensors', 'vocab.txt'])" \
 && refs="$HF_HOME/hub/models--$(echo "$MODEL" | sed 's#/#--#')/refs" \
 && mkdir -p "$refs" && printf '%s' "$MODEL_REVISION" > "$refs/main"

COPY src/agent_notes /app/src/agent_notes

ENV PYTHONPATH=/app/src \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    AGENT_NOTES_EMBED_HOST=0.0.0.0 \
    AGENT_NOTES_EMBED_PORT=8766

# Fail the build if the baked model can't load offline.
RUN python -c "from agent_notes.core.embed import embed_local; print(embed_local('build check', 'query').shape)"

USER 10001:10001
EXPOSE 8766
ENTRYPOINT ["python", "-m", "agent_notes.embed_server"]
