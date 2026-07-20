# Deploy any component as service or agent automatically


## Full Service

```bash

./deploy-local.sh \
   services=order_service:8000,inventory_service:8001,pricing_service:8002,shipment_service:8006,shopping_cart_service:8003,shipment_service:8006,payment_service:8007,product_catalog_service:8008,procurement_service:8009,subscription_service:8010 \
   agents=

./shutdown-local.sh \
   services=order_service:8000,inventory_service:8001,pricing_service:8002,shipment_service:8006,shopping_cart_service:8003,shipment_service:8006 payment_service:8007,product_catalog_service:8008,procurement_service:8009,subscription_service:8010 \
   agents=

```

## Full Agent

``` bash

./deploy-local.sh \
   services= \
   agents=order_agent_new:8000,inventory_agent:8001,pricing_agent:8002,shipment_agent:8006,shopping_cart_agent:8003,shipment_agent:8006 payment_agent:8007,product_catalog_agent:8008,procurement_agent:8009,subscription_agent:8010

./shutdown-local.sh \
   services= \
   agents=order_agent_new:8000,inventory_agent:8001,pricing_agent:8002,shipment_agent:8006,shopping_cart_agent:8003,shipment_agent:8006 payment_agent:8007,product_catalog_agent:8008,procurement_agent:8009,subscription_agent:8010

```


## Only Orchestrator Agent + All other services

```bash

./deploy-local.sh \
   services=inventory_service:8001,pricing_service:8002,shipment_service:8006,shopping_cart_service:8003,shipment_service:8006,payment_service:8007,product_catalog_service:8008,procurement_service:8009,subscription_service:8010 \
   agents=order_agent_new:8000

./shutdown-local.sh \
   services=inventory_service:8001,pricing_service:8002,shipment_service:8006,shopping_cart_service:8003,shipment_service:8006,payment_service:8007,product_catalog_service:8008,procurement_service:8009,subscription_service:8010 \
   agents=order_agent_new:8000

```

## Orchestrator service + All Specialized agents

``` bash

./deploy-local.sh \
   services=order_service:8000 \
   agents=inventory_agent:8001,pricing_agent:8002,shipment_agent:8006,shopping_cart_agent:8003,shipment_agent:8006,payment_agent:8007,product_catalog_agent:8008,procurement_agent:8009,subscription_agent:8010

./shutdown-local.sh \
   services=order_service:8000 \
   agents=inventory_agent:8001,pricing_agent:8002,shipment_agent:8006,shopping_cart_agent:8003,shipment_agent:8006,payment_agent:8007,product_catalog_agent:8008,procurement_agent:8009,subscription_agent:8010


```

## Hybrid at each step

``` bash

./deploy-local.sh \
   services=order_service:8000,inventory_service:8001,pricing_service:8002 \
   agents=shipment_agent:8006,shopping_cart_agent:8003,shipment_agent:8006,payment_agent:8007,product_catalog_agent:8008,procurement_agent:8009 subscription_agent:8010

./shutdown-local.sh \
   services=order_service:8000,inventory_service:8001,pricing_service:8002 \
   agents=shipment_agent:8006,shopping_cart_agent:8003,shipment_agent:8006,payment_agent:8007,product_catalog_agent:8008,procurement_agent:8009,subscription_agent:8010

```