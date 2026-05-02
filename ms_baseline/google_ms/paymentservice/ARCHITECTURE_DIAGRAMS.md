# PaymentService MongoDB Integration - Architecture Diagram

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          Payment Service Architecture                       │
└─────────────────────────────────────────────────────────────────────────────┘

                            ┌─────────────────────┐
                            │   gRPC Client       │
                            │  (Checkout Service) │
                            └──────────┬──────────┘
                                       │ ChargeRequest
                                       │ {credit_card, amount}
                                       ▼
                    ┌──────────────────────────────────────┐
                    │   PaymentService gRPC Servicer       │
                    │                                      │
                    │  Charge(request, context) RPC        │
                    │  ┌────────────────────────────────┐  │
                    │  │ 1. Generate UUID payment_id    │  │
                    │  │ 2. Extract request data        │  │
                    │  │ 3. Validate credit card        │  │
                    │  │ 4. On Success:                 │  │
                    │  │    ├─ Extract response data    │  │
                    │  │    └─ Persist to MongoDB ──────┼──┼──┐
                    │  │ 5. On Error:                   │  │  │
                    │  │    └─ Persist error to MongoDB ┼──┼──┤
                    │  │ 6. Return ChargeResponse       │  │  │
                    │  └────────────────────────────────┘  │  │
                    └──────────────────────────────────────┘  │
                                       │                      │
                            ChargeResponse                    │
                            {transaction_id}                  │
                                       │                      │
                                       ▼                      │
                            ┌─────────────────────┐           │
                            │   gRPC Client       │           │
                            │   (responds)        │           │
                            └─────────────────────┘           │
                                                              │
                                                    save_charge_transaction()
                                                    (Async, Non-blocking)
                                                              │
                                    ┌─────────────────────────▼──────────────┐
                                    │   MongoDB Async Driver (Motor)         │
                                    │   AsyncIOMotorClient                   │
                                    └─────────────────────────┬──────────────┘
                                                              │
                                    ┌─────────────────────────▼──────────────┐
                                    │   MongoDB Server                       │
                                    │   (mongodb://localhost:27017)          │
                                    │                                        │
                                    │  Database: paymentservice              │
                                    │  Collection: payment_transactions      │
                                    │                                        │
                                    │  Document:                             │
                                    │  {                                     │
                                    │    _id: payment_id (UUID)             │
                                    │    payment_id: UUID                    │
                                    │    created_at: timestamp               │
                                    │    status: "success" | "failed"        │
                                    │    request: {...}                      │
                                    │    response: {...} | error: "..."      │
                                    │  }                                     │
                                    └────────────────────────────────────────┘
```

## Request Flow Diagram

```
┌────────────────────────────────────────────────────────────────────────────┐
│ SUCCESS PATH                                                               │
├────────────────────────────────────────────────────────────────────────────┤

  Client (checkout)
      │
      │ ChargeRequest
      ├─ credit_card_number: "4432801561520454"
      ├─ credit_card_cvv: "123"
      ├─ expiration: 12/2025
      └─ amount: USD 100.50
              │
              ▼
  ┌──────────────────────────┐
  │ PaymentServicer.Charge() │
  └────────┬─────────────────┘
           │
           ├─ Generate payment_id: "550e8400-e29b-41d4-a716-446655440000"
           │
           ├─ Validate card: VALID
           │
           ├─ Extract response data:
           │  ├─ transaction_id: UUID
           │  ├─ card_type: "Visa"
           │  └─ last_four: "4242"
           │
           ├─ save_charge_transaction(payment_id, request_data, response_data)
           │  │
           │  └─ Insert to MongoDB:
           │     {
           │       "_id": "550e8400-e29b-41d4-a716-446655440000",
           │       "status": "success",
           │       "request": {...},
           │       "response": {
           │         "transaction_id": "...",
           │         "card_type": "Visa",
           │         "last_four": "4242"
           │       }
           │     }
           │
           └─ Return ChargeResponse(transaction_id)
                   │
                   ▼
           Response to Client
           "Transaction Successful"


┌────────────────────────────────────────────────────────────────────────────┐
│ ERROR PATH (Invalid Card)                                                 │
├────────────────────────────────────────────────────────────────────────────┤

  Client (checkout)
      │
      │ ChargeRequest
      └─ credit_card_number: "4222222222222220"  ← Invalid
              │
              ▼
  ┌──────────────────────────┐
  │ PaymentServicer.Charge() │
  └────────┬─────────────────┘
           │
           ├─ Generate payment_id: "550e8400-e29b-41d4-a716-446655440001"
           │
           ├─ Validate card: INVALID
           │
           ├─ CardValidationError exception raised
           │
           ├─ save_charge_transaction(
           │    payment_id,
           │    request_data,
           │    error="Card declined",
           │    error_code="INVALID_ARGUMENT"
           │  )
           │  │
           │  └─ Insert to MongoDB:
           │     {
           │       "_id": "550e8400-e29b-41d4-a716-446655440001",
           │       "status": "failed",
           │       "error": "Card declined",
           │       "error_code": "INVALID_ARGUMENT",
           │       "request": {...}
           │     }
           │
           └─ Abort gRPC call with INVALID_ARGUMENT
                   │
                   ▼
           Error Response to Client
           "Card validation failed"
```

## MongoDB Document Structure

### Success Document

```javascript
{
  "_id": "550e8400-e29b-41d4-a716-446655440000",
  "payment_id": "550e8400-e29b-41d4-a716-446655440000",
  "created_at": ISODate("2024-01-15T10:30:45.123Z"),
  "status": "success",
  "request": {
    "amount": {
      "currency_code": "USD",
      "units": 100,
      "nanos": 500000000
    },
    "credit_card": {
      "number_ending": "4242",
      "expiration_month": 12,
      "expiration_year": 2025
    }
  },
  "response": {
    "transaction_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
    "card_type": "Visa",
    "last_four": "4242"
  }
}
```

### Failure Document

```javascript
{
  "_id": "550e8400-e29b-41d4-a716-446655440001",
  "payment_id": "550e8400-e29b-41d4-a716-446655440001",
  "created_at": ISODate("2024-01-15T10:31:22.456Z"),
  "status": "failed",
  "error": "Invalid card number",
  "error_code": "INVALID_ARGUMENT",
  "request": {
    "amount": {
      "currency_code": "USD",
      "units": 50,
      "nanos": 0
    },
    "credit_card": {
      "number_ending": "2220",
      "expiration_month": 12,
      "expiration_year": 2025
    }
  }
}
```

## Async Flow Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    Async Charge Processing Timeline                         │
└─────────────────────────────────────────────────────────────────────────────┘

Time     Event                          gRPC Response      MongoDB Status
────     ─────                          ─────────────      ──────────────
0ms  ┌─  Request received
     │   payment_id generated
     │
5ms  │   Card validation: OK
     │
10ms │   Response data extracted
     │
     ├─→ save_charge_transaction() called (async) ─────────────────────┐
     │                                                                  │
15ms │   ✓ ChargeResponse returned to client                           │
     │   (gRPC call completes)                                         │
     │                                                                  │
20ms │                                          ┌──────────────────────┤
     │                                          │ MongoDB insert queued │
     │                                          │
30ms │                                          │ Network latency
     │                                          │
45ms │                                          │ Document inserted
     │                                          ├─────────────────────
     │                                          ✓ Persisted
     │
─────┴──────────────────────────────────────────────────────────────────
     ^ gRPC response is fast
       (not blocked by MongoDB)
       
     ^ MongoDB persistence happens in background
       (non-blocking, async)
```

## Collection Index Structure

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    MongoDB Indexes on payment_transactions                  │
└─────────────────────────────────────────────────────────────────────────────┘

┌─ _id (Primary Key)
│  Type: Unique Index (B-tree)
│  Use: Fast document lookup by payment_id
│  Lookup: O(log n)
│  Size: ~50 bytes per doc
│
├─ payment_id (Unique Index)
│  Type: Unique Hash Index
│  Use: Ensure no duplicate payments
│  Lookup: O(1)
│  Size: ~50 bytes per doc
│
└─ created_at (Compound Index)
   Type: B-tree Ascending
   Use: Time-range queries, sorting
   Lookup: O(log n)
   Size: ~20 bytes per doc
   Query Examples:
     - Find transactions from last hour
     - Sort by timestamp
     - Count by date
     - Aggregate over time periods
```

## Error Handling Flow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                   Error Handling and Persistence                            │
└─────────────────────────────────────────────────────────────────────────────┘

Try:
  ├─ charge() validation
  │
  ├─ On CardValidationError
  │  ├─ Log warning with payment_id
  │  ├─ save_charge_transaction(..., error=str(e), error_code="INVALID_ARGUMENT")
  │  │  ├─ Create document with error field
  │  │  ├─ Set status="failed"
  │  │  ├─ Insert to MongoDB (async)
  │  │  └─ Log result
  │  ├─ context.abort(INVALID_ARGUMENT, str(e))
  │  └─ Return empty ChargeResponse
  │
  └─ On Generic Exception
     ├─ Log error with payment_id and traceback
     ├─ save_charge_transaction(..., error=str(e), error_code="INTERNAL")
     │  ├─ Create document with error field
     │  ├─ Set status="failed"
     │  ├─ Insert to MongoDB (async)
     │  └─ Log result
     ├─ context.abort(INTERNAL, "Unexpected error: ...")
     └─ Return empty ChargeResponse

MongoDB Persistence Failure:
  └─ Logged but doesn't affect gRPC response
     (Graceful degradation)
```

## Deployment Topology

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                       Production Deployment                                 │
└─────────────────────────────────────────────────────────────────────────────┘

Services (Kubernetes / Docker Compose)
│
├─ Frontend (Browsers)
│  └─ Checkout Service
│     └─ gRPC call to Payment Service
│
├─ PaymentService Replica 1 ─────┐
│  ├─ gRPC server on port 5052   │
│  ├─ HTTP proxy on port 8052    │
│  └─ MongoDB client (Motor)     │
│                                 │
├─ PaymentService Replica 2 ─────┤
│  ├─ gRPC server on port 5052   │
│  ├─ HTTP proxy on port 8052    │ ──┐
│  └─ MongoDB client (Motor)     │  │
│                                 │  │ All replicas write to same DB
├─ PaymentService Replica N ─────┤  │
│  ├─ gRPC server on port 5052   │  │
│  ├─ HTTP proxy on port 8052    │  │
│  └─ MongoDB client (Motor)     │  │
│                                 │  │
└────────────────────────────────┼──┘
                                  │
                   Load Balancer  │
                        (L4/L7)   │
                                  │
                                  ▼
                    ┌──────────────────────────────┐
                    │   MongoDB Server (Cluster)   │
                    │                              │
                    ├─ Primary Node               │
                    │  └─ Accepts writes          │
                    │                              │
                    ├─ Secondary Node 1           │
                    │  └─ Replicates writes       │
                    │                              │
                    └─ Secondary Node 2           │
                       └─ Replicates writes       │
                                  │
                    Backup & Archival
                       (Optional)
```

## Performance Timeline

```
Operation Timeline (typical values):

gRPC Channel Setup:          0-100ms (one-time, connection pooled)
Card Validation:             1-5ms (in-memory, crypto operations)
MongoDB Connection Pooling:  0ms (connection reused)
MongoDB Insert:              2-10ms (depends on network, disk)
gRPC Response Serialization: <1ms
─────────────────────────────────────────────
Total gRPC Response Time:    5-10ms (blocking only validation)
MongoDB Persistence:         2-10ms (non-blocking, concurrent)
```

## Data Flow Summary

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          Complete Data Flow                                 │
└─────────────────────────────────────────────────────────────────────────────┘

Input:  ChargeRequest
        ├─ credit_card: CreditCardInfo
        │  ├─ number (encrypted or secure)
        │  ├─ cvv
        │  ├─ expiration_month
        │  └─ expiration_year
        └─ amount: Money
           ├─ currency_code
           ├─ units
           └─ nanos

Processing:
        ├─ Generate UUID payment_id
        ├─ Sanitize card (keep last 4 only)
        ├─ Validate credit card
        │
        ├─ Success Path:
        │  ├─ Extract transaction_id
        │  ├─ Detect card_type
        │  └─ Persist to MongoDB with response_data
        │
        └─ Error Path:
           ├─ Capture error message
           ├─ Map to error_code
           └─ Persist to MongoDB with error field

Output: ChargeResponse (via gRPC)
        └─ transaction_id (on success) or
           gRPC error (on failure)

Database: MongoDB Document
        ├─ _id: payment_id
        ├─ created_at: timestamp
        ├─ status: "success" | "failed"
        ├─ request: {...sanitized input...}
        ├─ response: {...transaction details...} OR
        └─ error: {...error details...}
```

## Concurrent Request Handling

```
┌─────────────────────────────────────────────────────────────────────────────┐
│              Multiple Concurrent Charge Requests                            │
└─────────────────────────────────────────────────────────────────────────────┘

Request Timeline:

Time │ Client 1            │ Client 2            │ Client 3
─────┼─────────────────────┼─────────────────────┼─────────────────
0ms  │ Charge #1           │ Charge #2           │ Charge #3
     │ payment_id=uuid1    │ payment_id=uuid2    │ payment_id=uuid3
     │
5ms  │ Validate            │ Validate            │ Validate
     │
10ms │ ✓ Response #1       │ ✓ Response #2       │ ✓ Response #3
     │ (no wait)           │ (no wait)           │ (no wait)
     │
     │ (Background)
     ├─────────────────────┴─────────────────────┴─────────────────
     │  MongoDB writes happening concurrently
     │
20ms │ Doc #1 persisted    │
30ms │                     │ Doc #2 persisted    │
35ms │                     │                     │ Doc #3 persisted

Result: All clients get immediate response
        All documents eventually persisted
        (Not sequential - concurrent)
```

This architecture provides:
✅ Fast gRPC responses (validation only, no DB blocking)
✅ Persistent audit trail (all charges recorded)
✅ Concurrent request handling (async MongoDB I/O)
✅ Error tracking (validation and system errors)
✅ Queryable history (indexed MongoDB collection)
✅ Scalability (stateless servicers, shared MongoDB)
✅ Compliance (PCI-DSS: no full card numbers)
