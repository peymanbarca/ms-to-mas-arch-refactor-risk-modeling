# Deploy any component as service or agent automatically


## Full Service

```bash

./deploy-local.sh \
  services=ad_service:5057,cart_service:5054,checkout_service:5050,currency_service:5053,email_service:5056,payment_service:5052,product_catalog_service:5055,recommendation_service:5058,shipping_service:5051 \
  agents=

./shutdown-local.sh \
  services=ad_service:5057,cart_service:5054,checkout_service:5050,currency_service:5053,email_service:5056,payment_service:5052,product_catalog_service:5055,recommendation_service:5058,shipping_service:5051 \
  agents=

```

## Full Agent

``` bash

./deploy-local.sh \
  services= \
  agents=ad_agent:5057,cart_agent:5054,checkout_agent:5050,currency_agent:5053,email_agent:5056,payment_agent:5052,product_catalog_agent:5055,recommendation_agent:5058,shipping_agent:5051

./shutdown-local.sh \
  services= \
  agents=ad_agent:5057,cart_agent:5054,checkout_agent:5050,currency_agent:5053,email_agent:5056,payment_agent:5052,product_catalog_agent:5055,recommendation_agent:5058,shipping_agent:5051

```


## Only Orchestrator Agent + All other services

```bash

./deploy-local.sh \
  services=ad_service:5057,cart_service:5054,currency_service:5053,email_service:5056,payment_service:5052,product_catalog_service:5055,recommendation_service:5058,shipping_service:5051 \
  agents=checkout_agent:5050

./shutdown-local.sh \
  services=ad_service:5057,cart_service:5054,currency_service:5053,email_service:5056,payment_service:5052,product_catalog_service:5055,recommendation_service:5058,shipping_service:5051 \
  agents=checkout_agent:5050

```

## Orchestrator service + All Specialized agents

``` bash

./deploy-local.sh \
  services=checkout_service:5050 \
  agents=ad_agent:5057,cart_agent:5054,currency_agent:5053,email_agent:5056,payment_agent:5052,product_catalog_agent:5055,recommendation_agent:5058,shipping_agent:5051

./shutdown-local.sh \
  services=checkout_service:5050 \
  agents=ad_agent:5057,cart_agent:5054,currency_agent:5053,email_agent:5056,payment_agent:5052,product_catalog_agent:5055,recommendation_agent:5058,shipping_agent:5051

```

## Hybrid at each step

``` bash

./deploy-local.sh \
  services=checkout_service:5050,ad_service:5057,cart_service:5054, \
  agents=currency_agent:5053,email_agent:5056,payment_agent:5052,product_catalog_agent:5055,recommendation_agent:5058,shipping_agent:5051

./shutdown-local.sh \
  services=checkout_service:5050,ad_service:5057,cart_service:5054, \
  agents=currency_agent:5053,email_agent:5056,payment_agent:5052,product_catalog_agent:5055,recommendation_agent:5058,shipping_agent:5051

```