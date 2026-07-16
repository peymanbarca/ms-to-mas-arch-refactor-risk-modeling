#!/bin/bash

# Example usage:
#./deploy-local.sh \
#   services=home_timeline_service:9099,media_service:9091,post_storage_service:9096,social_graph_service:9097,text_service:9095,unique_id_service:9090,url_shorten_service:9092,user_mention_service:9093,user_service:9094,user_timeline_service:9098,write_home_timeline_service:8999 \
#   agents=compose_post_agent:9100
#
# Full Services:
#./deploy-local.sh \
#   services=compose_post_service:9100,home_timeline_service:9099,media_service:9091,post_storage_service:9096,social_graph_service:9097,text_service:9095,unique_id_service:9090,url_shorten_service:9092,user_mention_service:9093,user_service:9094,user_timeline_service:9098,write_home_timeline_service:8999 \
#   agents=
#


# echo "Parsing arguments..."

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

# echo "Cleaning ports..."

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
sleep 0.1

echo "Starting services..."

cd ../.. && rm -f *.log

for pair in "${SVC_LIST[@]}"; do
  NAME="${pair%%:*}"
  PORT="${pair##*:}"

  echo "Running service: $NAME on port $PORT"
  if [ "$NAME" == "compose_post_service" ]; then
     nohup python3 -m ms_baseline.dsb_social.compose_post_service.server >& compose_post_service.log &
  elif [ "$NAME" == "home_timeline_service" ]; then
     nohup python3 -m ms_baseline.dsb_social.home_timeline_service.server >& home_timeline_service.log &
  elif [ "$NAME" == "media_service" ]; then  
     nohup python3 -m ms_baseline.dsb_social.media_service.server >& media_service.log &
  elif [ "$NAME" == "post_storage_service" ]; then  
     nohup python3 -m ms_baseline.dsb_social.post_storage_service.server >& post_storage_service.log &
  elif [ "$NAME" == "social_graph_service" ]; then  
     nohup python3 -m ms_baseline.dsb_social.social_graph_service.server >& social_graph_service.log &
  elif [ "$NAME" == "text_service" ]; then  
     nohup python3 -m ms_baseline.dsb_social.text_service.server >& text_service.log &
  elif [ "$NAME" == "unique_id_service" ]; then  
     nohup python3 -m ms_baseline.dsb_social.unique_id_service.server >& unique_id_service.log &
  elif [ "$NAME" == "url_shorten_service" ]; then  
     nohup python3 -m ms_baseline.dsb_social.url_shorten_service.server >& url_shorten_service.log &
  elif [ "$NAME" == "user_mention_service" ]; then  
     nohup python3 -m ms_baseline.dsb_social.user_mention_service.server >& user_mention_service.log &
  elif [ "$NAME" == "user_service" ]; then  
     nohup python3 -m ms_baseline.dsb_social.user_service.server >& user_service.log &
  elif [ "$NAME" == "user_timeline_service" ]; then  
     nohup python3 -m ms_baseline.dsb_social.user_timeline_service.server >& user_timeline_service.log &
  elif [ "$NAME" == "write_home_timeline_service" ]; then  
     nohup python3 -m ms_baseline.dsb_social.write_home_timeline_service.server >& write_home_timeline_service.log &
  else
     echo "Unknown service: $NAME"
  fi
done




echo "Starting agents..."

for pair in "${AGENT_LIST[@]}"; do
  NAME="${pair%%:*}"
  PORT="${pair##*:}"

  echo "Running service: $NAME on port $PORT"
  if [ "$NAME" == "compose_post_agent" ]; then
     nohup python3 -m refactored_architecture.dsb_social.compose_post_agent.server >& compose_post_agent.log &
  elif [ "$NAME" == "home_timeline_agent" ]; then
     nohup python3 -m refactored_architecture.dsb_social.home_timeline_agent.server >& home_timeline_agent.log &
  elif [ "$NAME" == "media_agent" ]; then  
     nohup python3 -m refactored_architecture.dsb_social.media_agent.server >& media_agent.log &
  elif [ "$NAME" == "post_storage_agent" ]; then  
     nohup python3 -m refactored_architecture.dsb_social.post_storage_agent.server >& post_storage_agent.log &
  elif [ "$NAME" == "social_graph_agent" ]; then  
     nohup python3 -m refactored_architecture.dsb_social.social_graph_agent.server >& social_graph_agent.log &
  elif [ "$NAME" == "text_agent" ]; then  
     nohup python3 -m refactored_architecture.dsb_social.text_agent.server >& text_agent.log &
  elif [ "$NAME" == "unique_id_agent" ]; then  
     nohup python3 -m refactored_architecture.dsb_social.unique_id_agent.server >& unique_id_agent.log &
  elif [ "$NAME" == "url_shorten_agent" ]; then  
     nohup python3 -m refactored_architecture.dsb_social.url_shorten_agent.server >& url_shorten_agent.log &
  elif [ "$NAME" == "user_mention_agent" ]; then  
     nohup python3 -m refactored_architecture.dsb_social.user_mention_agent.server >& user_mention_agent.log &
  elif [ "$NAME" == "user_agent" ]; then  
     nohup python3 -m refactored_architecture.dsb_social.user_agent.server >& user_agent.log &
  elif [ "$NAME" == "user_timeline_agent" ]; then  
     nohup python3 -m refactored_architecture.dsb_social.user_timeline_agent.server >& user_timeline_agent.log &
  elif [ "$NAME" == "write_home_timeline_agent" ]; then  
     nohup python3 -m refactored_architecture.dsb_social.write_home_timeline_agent.server >& write_home_timeline_agent.log &
  else
     echo "Unknown service: $NAME"
  fi
done

# Graceful shutdown on CTRL+C
trap "echo 'Stopping all...'; kill 0" SIGINT

#wait