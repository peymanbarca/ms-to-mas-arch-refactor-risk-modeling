#!/bin/bash



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
  PORT_REST=$((PORT + 1000)) # Assuming REST port is 1000 more than gRPC port
  kill_port $PORT
  kill_port $PORT_REST
done

# Kill agent ports
IFS=',' read -ra AGENT_LIST <<< "$AGENTS"
for pair in "${AGENT_LIST[@]}"; do
  NAME="${pair%%:*}"
  PORT="${pair##*:}"
  PORT_REST=$((PORT + 1000)) # Assuming REST port is 1000 more than gRPC port
  kill_port $PORT
  kill_port $PORT_REST
done


cd ../.. && rm *.log

# Graceful shutdown on CTRL+C
trap "echo 'Stopping all...'; kill 0" SIGINT
