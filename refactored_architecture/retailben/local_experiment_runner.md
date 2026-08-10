

# 1. Inventory Agent

## Set service up:

```bash
nohup python3 run_as_service.py inventory_agent 8001 >& inventory_agent.log &
```

## Send a request to service:

``` bash

curl -X 'POST' 'http://127.0.0.1:8001/reset_stocks' -H 'accept: application/json' -H 'Content-Type: application/json' -d '{"items": [{"sku": "4cc0770f-91bc-4c0d-a26f-7b872f02ca94","stock": 10}]}'

curl -X 'POST' 'http://127.0.0.1:8001/reset_stocks' -H 'accept: application/json' -H 'Content-Type: application/json' -d '{"items": [{"sku": "b2926dc2-cc6d-4c3e-ae40-7a127c173b16","stock": 10}]}'

curl -X 'POST' 'http://127.0.0.1:8001/reserve' -H 'accept: application/json' -H 'Content-Type: application/json' -d '{"order_id":"a", "items": [{"sku": "4cc0770f-91bc-4c0d-a26f-7b872f02ca94","qty": 2}]}'
```

------------------------

# 2. Order Agent

## Set service up:

```bash
nohup python3 run_as_service.py order_agent 8000 >& order_agent.log &
```

## Send a request to service:

``` bash

curl -X 'POST' 'http://127.0.0.1:8000/cart/{CART_ID}/checkout'
```

------------------------

# 3. Payment Agent

## Set service up:

```bash
nohup python3 run_as_service.py payment_agent 8007 >& payment_agent.log &
```

## Send a request to service:

``` bash

curl -X 'POST' 'http://127.0.0.1:8007/pay-order' -H 'accept: application/json' -H 'Content-Type: application/json' -d '{"order_id":"a", "final_price": 2000}'
```

------------------------

# 4. Pricing Agent

## Set service up:

```bash
nohup python3 run_as_service.py pricing_agent 8002 >& pricing_agent.log &
```

## Send a request to service:

``` bash


curl -X 'POST' 'http://127.0.0.1:8002/price/put' -H 'accept: application/json' -H 'Content-Type: application/json' -d '{"product_id": "4cc0770f-91bc-4c0d-a26f-7b872f02ca94","price": 2000}'

curl -X 'POST' 'http://127.0.0.1:8002/price/put' -H 'accept: application/json' -H 'Content-Type: application/json' -d '{"product_id": "b2926dc2-cc6d-4c3e-ae40-7a127c173b16","price": 200}'

curl -X 'POST' 'http://127.0.0.1:8002/price' -H 'accept: application/json' -H 'Content-Type: application/json' -d '{"items": [{"product_id": "4cc0770f-91bc-4c0d-a26f-7b872f02ca94","qty": 2}],"promo_codes": [],"currency": "USD"}'

curl -X 'POST' 'http://127.0.0.1:8002/price' -H 'accept: application/json' -H 'Content-Type: application/json' -d '{"items": [{"product_id": "b2926dc2-cc6d-4c3e-ae40-7a127c173b16","qty": 2}],"promo_codes": [],"currency": "USD"}'

```

------------------------

# 5. Procurement Agent

## Set service up:

```bash
nohup python3 run_as_service.py procurement_agent 8009 >& procurement_agent.log &
```

## Send a request to service:

``` bash

curl -X 'POST' 'http://127.0.0.1:8009/order_supplier' -H 'accept: application/json' -H 'Content-Type: application/json' -d '{"sku": "4cc0770f-91bc-4c0d-a26f-7b872f02ca94","qty": 100, "preferred_supplier": "IGI"}'
```

------------------------

# 6. Product Search Agent

## Set service up:

```bash
nohup python3 run_as_service.py product_search_agent 8008 >& product_search_agent.log &
```

## Send a request to service:

``` bash

curl -X 'GET' 'http://127.0.0.1:8008/search?q=looking%20for%20headphone%20with%20noise%20cancelling%20under%20300$' -H 'accept: application/json'

```

------------------------

# 7. Shipment Agent

## Set service up:

```bash
nohup python3 run_as_service.py shipment_agent 8006 >& shipment_agent.log &
```

## Send a request to service:

``` bash

curl -X 'POST' 'http://127.0.0.1:8008/products' -H 'accept: application/json' -H 'Content-Type: application/json' -d '{"sku":"b2926dc2-cc6d-4c3e-ae40-7a127c173b16", "name": "Airpod", "description": "Noise cancelling headphone Airpod"}'

curl -X 'POST' 'http://127.0.0.1:8006/book' -H 'accept: application/json' -H 'Content-Type: application/json' -d '{"order_id":"a", "address": "abc"}'
```

------------------------

# 8. Shopping Cart Agent

## Set service up:

```bash
nohup python3 run_as_service.py shopping_cart_agent 8003 >& shopping_cart_agent.log &
```

## Send a request to service:

``` bash

curl -X 'POST' 'http://127.0.0.1:8003/cart/-1/items' -H 'accept: application/json' -H 'Content-Type: application/json' -d '{"sku":"4cc0770f-91bc-4c0d-a26f-7b872f02ca94", "qty": 2}'
curl -X 'GET' 'http://127.0.0.1:8003/cart/2c73d756-bf8f-4c60-99ca-47008f3721c4' -H 'accept: application/json'
```

------------------------

# 9. Subscription Agent

## Set service up:

```bash
nohup python3 run_as_service.py subscription_agent 8010 >& subscription_agent.log &
```

## Send a request to service:

``` bash

curl -X 'POST' 'http://127.0.0.1:8010/subscriptions' -H 'accept: application/json' -H 'Content-Type: application/json' -d '{"user_id": "1","email": "a@b.com", "promo_code": "SUMMER20"}'

curl -X 'GET' 'http://127.0.0.1:8010/subscriptions/1' -H 'accept: application/json' -H 'Content-Type: application/json' 

```

------------------------

