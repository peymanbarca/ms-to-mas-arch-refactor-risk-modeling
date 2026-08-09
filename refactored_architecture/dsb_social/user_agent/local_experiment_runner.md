

## Set service up:

```bash
python3 -m refactored_architecture.dsb_social.user_agent.server
```

## Send a request to service:

``` bash

python3 -m refactored_architecture.dsb_social.user_agent.client register --first Alice --last Smith --username alice --password secret123AB
python3 -m refactored_architecture.dsb_social.user_agent.client login --username alice --password secret123AB
python3 -m refactored_architecture.dsb_social.user_agent.client get-id --username alice
python3 -m refactored_architecture.dsb_social.user_agent.client compose-creator --username alice
```