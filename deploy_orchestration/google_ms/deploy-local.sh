#!/bin/bash

# Example usage:
#./deploy-local.sh \
#   services=ad_service:5057,product_catalog_service:5055 \
#   agents=cart_agent:5054,shipping_agent:5051,payment_agent:5052,checkout_agent:5050
#
# Full Services:
#./deploy-local.sh \
#   services=ad_service:5057,cart_service:5054,checkout_service:5050,currency_service:5053,email_service:5056,payment_service:5052,product_catalog_service:5055,recommendation_service:5058,shipping_service:5051 \
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
  PORT_REST=$((PORT + 1000)) # Assuming REST port is 1000 more than gRPC port

  echo "Running service: $NAME on port $PORT, Swagger UI: http://localhost:$PORT_REST/docs"
  if [ "$NAME" == "ad_service" ]; then
     nohup python3 -m ms_baseline.google_ms.adservice.adservice >& ad_service.log &
  elif [ "$NAME" == "cart_service" ]; then
     nohup python3 -m ms_baseline.google_ms.cartservice.cartservice >& cart_service.log &
  elif [ "$NAME" == "product_catalog_service" ]; then  
     nohup python3 -m ms_baseline.google_ms.productcatalog.productcatalogservice >& productcatalog_service.log &
  elif [ "$NAME" == "recommendation_service" ]; then  
     nohup python3 -m ms_baseline.google_ms.recommendationservice.recommendationservice >& recommendationservice.log &
  elif [ "$NAME" == "shipping_service" ]; then  
     nohup python3 -m ms_baseline.google_ms.shippingservice.shippingservice >& shippingservice.log &
  elif [ "$NAME" == "payment_service" ]; then  
     nohup python3 -m ms_baseline.google_ms.paymentservice.paymentservice >& payment_service.log &
  elif [ "$NAME" == "currency_service" ]; then  
     nohup python3 -m ms_baseline.google_ms.currencyservice.currencyservice >& currency_service.log &
  elif [ "$NAME" == "email_service" ]; then  
     nohup python3 -m ms_baseline.google_ms.emailservice.emailservice >& email_service.log &
  elif [ "$NAME" == "checkout_service" ]; then  
     nohup python3 -m ms_baseline.google_ms.checkoutservice.checkoutservice >& checkoutservice.log &
  else
     echo "Unknown service: $NAME"
  fi
done




echo "Starting agents..."

for pair in "${AGENT_LIST[@]}"; do
  NAME="${pair%%:*}"
  PORT="${pair##*:}"
  PORT_REST=$((PORT + 1000)) # Assuming REST port is 1000 more than gRPC port

  echo "Running agent: $NAME on port $PORT, Swagger UI: http://localhost:$PORT_REST/docs"
  if [ "$NAME" == "ad_agent" ]; then
     nohup python3 -m refactored_architecture.google_ms.adagent.adagent_as_service >& ad_agent.log &
  elif [ "$NAME" == "cart_agent" ]; then
     nohup python3 -m refactored_architecture.google_ms.cartagent.cartagent_as_service >& cart_agent.log &
  elif [ "$NAME" == "product_catalog_agent" ]; then  
     nohup python3 -m refactored_architecture.google_ms.productcatalogagent.productcatalogagent_as_service >& product_search_agent.log &
  elif [ "$NAME" == "recommendation_agent" ]; then  
     nohup python3 -m refactored_architecture.google_ms.recommendationagent.recommendationagent_as_service >& recommendationagent.log &
  elif [ "$NAME" == "shipping_agent" ]; then  
     nohup python3 -m refactored_architecture.google_ms.shippingagent.shippingagent_as_service >& shipment_agent.log &
  elif [ "$NAME" == "payment_agent" ]; then  
     nohup python3 -m refactored_architecture.google_ms.paymentagent.paymentagent_as_service >& payment_agent.log &
  elif [ "$NAME" == "currency_agent" ]; then  
     nohup python3 -m refactored_architecture.google_ms.currencyagent.currencyagent_as_service  >& currency_agent.log &
  elif [ "$NAME" == "email_agent" ]; then  
     nohup python3 -m refactored_architecture.google_ms.emailagent.emailagent_as_service  >& email_agent.log &
  elif [ "$NAME" == "checkout_agent" ]; then  
     nohup python3 -m refactored_architecture.google_ms.checkoutagent.checkoutagent_as_service >& checkout_agent.log &
  else
     echo "Unknown agent: $NAME"
  fi
done

# Graceful shutdown on CTRL+C
trap "echo 'Stopping all...'; kill 0" SIGINT

#wait