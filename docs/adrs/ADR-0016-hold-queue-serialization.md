# ADR-0016: Hold Queue Serialization

## Status

Accepted.

## Decision

Offering a returned or expired copy to the next waiting hold is serialized per `work_id` using a PostgreSQL advisory transaction lock.

## Consequences

Concurrent returns of the same Work cannot double-assign or skip the FIFO head merely because different copies were locked.

