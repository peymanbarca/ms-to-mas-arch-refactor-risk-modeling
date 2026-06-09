FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        thrift-compiler \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY social_network.thrift .
COPY message.py worker.py consumer.py publisher.py server.py tracing.py thrift_pool.py ./

RUN thrift --gen py social_network.thrift
RUN mkdir -p config

# No EXPOSE — this service does not listen on any port.
# It only connects outbound to RabbitMQ and HomeTimelineService.

ENTRYPOINT ["python", "server.py"]
CMD ["--config", "config/service-config.json"]
