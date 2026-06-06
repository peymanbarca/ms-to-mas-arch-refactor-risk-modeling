# Python port of DeathStarBench UniqueIdService
# Mirrors the original container structure so docker-compose.yml needs
# minimal changes (same hostname, same port 9090, same config volume mount).

FROM python:3.11-slim

# Install Thrift compiler so we can regenerate stubs inside the image
# (avoids shipping generated code).  For production you'd bake in the
# generated stubs instead.
RUN apt-get update && apt-get install -y --no-install-recommends \
        thrift-compiler \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY social_network.thrift .
COPY snowflake.py handler.py server.py tracing.py ./

# Generate Python Thrift stubs at image-build time
RUN thrift --gen py social_network.thrift

# Config is mounted at runtime (same as the C++ image)
# docker-compose: volumes: ["./config:/app/config"]
RUN mkdir -p config

EXPOSE 9090

# Entrypoint matches the original: entrypoint: UniqueIdService
# We name the script server.py but keep the same external interface.
ENTRYPOINT ["python", "server.py"]
CMD ["--config", "config/service-config.json"]
