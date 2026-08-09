

## Set service up:

```bash
python3 -m refactored_architecture.dsb_social.write_home_timeline_agent.server
```

## Send a request to service:

``` bash

python3 -m refactored_architecture.dsb_social.write_home_timeline_agent.publisher --post-id 1002 --user-id 2 --timestamp 1717000000000 --mentions 10 20

```