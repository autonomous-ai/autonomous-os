FROM pytorch/pytorch:2.4.0-cuda12.4-cudnn9-runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
    nginx daemontools \
    libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .

RUN pip install --no-cache-dir -e ".[dl,gpu,lb]"

EXPOSE 8899
ENV MODEL_CACHE_DIR=/workspace/models

# DL_API_KEY must be supplied at runtime:
#   docker run --gpus all -e DL_API_KEY=<secret> -p 8899:8899 dlbackend
#
# Roles (set via ROLE env var):
#   master  — nginx + lbserver + dlserver (default)
#   slave   — nginx + dlserver only (no LB, binds to :7999)
#
# See docker-compose.yml for multi-node setup.
# See dlbackend/docs/deployment.md for full documentation.

COPY scripts/docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

ENTRYPOINT ["/docker-entrypoint.sh"]
