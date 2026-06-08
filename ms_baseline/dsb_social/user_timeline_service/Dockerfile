FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        thrift-compiler \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY social_network.thrift .
COPY handler.py server.py tracing.py client.py thrift_pool.py ./

RUN thrift --gen py social_network.thrift
RUN mkdir -p config

EXPOSE 9098

ENTRYPOINT ["python", "server.py"]
CMD ["--config", "config/service-config.json"]
