#!/bin/bash

# Example usage:
#./deploy-local.sh \
#   services=order_service:8000,inventory_service:8001 \
#   agents=shopping_cart_agent:8003,shipment_agent:8006,payment_agent:8007
#
#./deploy-local.sh \
#   services=shipment_service:8006,payment_service:8007,shopping_cart_service:8003 \
#   agents=order_agent:8000,inventory_agent:8001

echo "Parsing arguments..."

for arg in "$@"; do
  case $arg in
    services=*)
      SERVICES="${arg#*=}"
      ;;
    agents=*)
      AGENTS="${arg#*=}"
      ;;
  esac
done

# 🔴 Kill processes on specified ports
kill_port () {
  PORT=$1
  PID=$(lsof -ti tcp:$PORT)

  if [ ! -z "$PID" ]; then
    echo "Killing process on port $PORT (PID=$PID)"
    kill -9 $PID
  fi
}

echo "Cleaning ports..."

# Kill service ports
IFS=',' read -ra SVC_LIST <<< "$SERVICES"
for pair in "${SVC_LIST[@]}"; do
  NAME="${pair%%:*}"
  PORT="${pair##*:}"
  kill_port $PORT
done

# Kill agent ports
IFS=',' read -ra AGENT_LIST <<< "$AGENTS"
for pair in "${AGENT_LIST[@]}"; do
  NAME="${pair%%:*}"
  PORT="${pair##*:}"
  kill_port $PORT
done

# Optional: small delay to release ports
sleep 1

echo "Starting services..."

cd ms_baseline && rm *.log

for pair in "${SVC_LIST[@]}"; do
  NAME="${pair%%:*}"
  PORT="${pair##*:}"

  echo "Running service: $NAME on port $PORT, Swagger UI: http://localhost:$PORT/docs"
  nohup python3 run_service.py "$NAME" "$PORT" >& "$NAME".log &
done


cd ../refactored_architecture && rm *.log

echo "Starting agents..."

for pair in "${AGENT_LIST[@]}"; do
  NAME="${pair%%:*}"
  PORT="${pair##*:}"

  echo "Running agent: $NAME on port $PORT, Swagger UI: http://localhost:$PORT/docs"
  nohup python3 run_as_service.py "$NAME" "$PORT" >& "$NAME".log &
done

# Graceful shutdown on CTRL+C
trap "echo 'Stopping all...'; kill 0" SIGINT

#wait