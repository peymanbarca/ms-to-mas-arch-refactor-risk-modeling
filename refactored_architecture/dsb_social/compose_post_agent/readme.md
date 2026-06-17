## High-Level ComposePost Agent (ReAct)

┌─────────────────────────────────────────────────────────────┐
│                    ComposePost Thrift API                   │
│                                                             │
│ ComposePost(req_id, username, user_id, text, media_ids...)  │
└───────────────────────┬─────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────┐
│                ComposePost Agent State                      │
│                                                             │
│ Inputs:                                                     │
│   req_id                                                    │
│   username                                                  │
│   user_id                                                   │
│   text                                                      │
│   media_ids                                                 │
│   media_types                                               │
│   post_type                                                 │
│                                                             │
│ Working Memory:                                             │
│   post_id                                                   │
│   text_result                                               │
│   creator                                                   │
│   media_list                                                │
│   post                                                      │
│   timeline_written                                          │
│   storage_written                                           │
│   home_timeline_sent                                        │
└───────────────────────┬─────────────────────────────────────┘
                        │
                        ▼
              ┌────────────────────┐
              │      REASON        │
              │      (LLM)         │
              └─────────┬──────────┘
                        │
                        │
                        ▼
              ┌────────────────────┐
              │   Select Tool      │
              └─────────┬──────────┘
                        │
                        ▼
              ┌────────────────────┐
              │      ACT           │
              │    (Tool Call)     │
              └─────────┬──────────┘
                        │
                        ▼
              ┌────────────────────┐
              │ Update State       │
              └─────────┬──────────┘
                        │
                        │
              Done ? ───┤── No ──► Back to REASON
                        │
                        ▼
                      END


##  At every ReAct iteration the LLM receives:
    Current State S
    +
    Available Tools T
    +
    Goal:
    "Successfully compose and publish a post"

and produces:

    Thought:
    What information is still missing?

    Action:
    Which tool should be called next?

    Observation:
    Tool result

    Updated State


--------------------------


                  ComposePost Agent
                  (LangGraph + LLM)

      Reason → Tool → Observe → Update State
             ↑                         │
             └──────── Loop ───────────┘

 Tools:
   • UniqueIdService (Thrift)
   • TextService (Thrift)
   • UserService (Thrift)
   • MediaService (Thrift)
   • PostStorageService (Thrift)
   • UserTimelineService (Thrift)
   • RabbitMQ Publisher

                ↓

     Same downstream interfaces
     Same storage systems
     Same message schemas

                ↓

      Only orchestration logic
      becomes agentic