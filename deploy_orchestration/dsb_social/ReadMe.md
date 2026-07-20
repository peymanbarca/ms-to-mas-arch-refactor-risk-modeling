# Deploy any component as service or agent automatically


## Full Service

```bash

./deploy-local.sh \
   services=compose_post_service:9100,home_timeline_service:9099,media_service:9091,post_storage_service:9096,social_graph_service:9097,text_service:9095,unique_id_service:9090,url_shorten_service:9092,user_mention_service:9093,user_service:9094,user_timeline_service:9098,write_home_timeline_service:8999 \
   agents=

./shutdown-local.sh \
   services=compose_post_service:9100,home_timeline_service:9099,media_service:9091,post_storage_service:9096,social_graph_service:9097,text_service:9095,unique_id_service:9090,url_shorten_service:9092,user_mention_service:9093,user_service:9094,user_timeline_service:9098,write_home_timeline_service:8999 \
   agents=

```

## Full Agent

``` bash

./deploy-local.sh \
   services= \
   agents=compose_post_agent:9100,home_timeline_agent:9099,media_agent:9091,post_storage_agent:9096,social_graph_agent:9097,text_agent:9095,unique_id_agent:9090,url_shorten_agent:9092,user_mention_agent:9093,user_agent:9094,user_timeline_agent:9098,write_home_timeline_agent:8999

./shutdown-local.sh \
   services= \
   agents=compose_post_agent:9100,home_timeline_agent:9099,media_agent:9091,post_storage_agent:9096,social_graph_agent:9097,text_agent:9095,unique_id_agent:9090,url_shorten_agent:9092,user_mention_agent:9093,user_agent:9094,user_timeline_agent:9098,write_home_timeline_agent:8999

```


## Only Orchestrator Agent + All other services

```bash

./deploy-local.sh \
   services=home_timeline_service:9099,media_service:9091,post_storage_service:9096,social_graph_service:9097,text_service:9095,unique_id_service:9090,url_shorten_service:9092,user_mention_service:9093,user_service:9094,user_timeline_service:9098,write_home_timeline_service:8999 \
   agents=compose_post_agent:9100

./shutdown-local.sh \
   services=home_timeline_service:9099,media_service:9091,post_storage_service:9096,social_graph_service:9097,text_service:9095,unique_id_service:9090,url_shorten_service:9092,user_mention_service:9093,user_service:9094,user_timeline_service:9098,write_home_timeline_service:8999 \
   agents=compose_post_agent:9100

```

## Orchestrator service + All Specialized agents

``` bash

./deploy-local.sh \
   services=compose_post_service:9100 \
   agents=home_timeline_agent:9099,media_agent:9091,post_storage_agent:9096,social_graph_agent:9097,text_agent:9095,unique_id_agent:9090,url_shorten_agent:9092,user_mention_agent:9093,user_agent:9094,user_timeline_agent:9098,write_home_timeline_agent:8999

./shutdown-local.sh \
   services=compose_post_service:9100 \
   agents=home_timeline_agent:9099,media_agent:9091,post_storage_agent:9096,social_graph_agent:9097,text_agent:9095,unique_id_agent:9090,url_shorten_agent:9092,user_mention_agent:9093,user_agent:9094,user_timeline_agent:9098,write_home_timeline_agent:8999

```

## Hybrid at each step

``` bash

./deploy-local.sh \
   services=compose_post_service:9100,home_timeline_service:9099,media_service:9091 \
   agents=post_storage_agent:9096,social_graph_agent:9097,text_agent:9095,unique_id_agent:9090,url_shorten_agent:9092,user_mention_agent:9093,user_agent:9094,user_timeline_agent:9098,write_home_timeline_agent:8999

./shutdown-local.sh \
   services=compose_post_service:9100,home_timeline_service:9099,media_service:9091 \
   agents=post_storage_agent:9096,social_graph_agent:9097,text_agent:9095,unique_id_agent:9090,url_shorten_agent:9092,user_mention_agent:9093,user_agent:9094,user_timeline_agent:9098,write_home_timeline_agent:8999

```