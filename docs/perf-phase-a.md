# Phase A performance pass

How much traffic the real stack carries, where it was losing time, and what changed. Every number
comes from a saved run in `perf/results/`; each row is the mean of two runs on the same machine.

**Setup:** `python -m perf.bench` drives `/checkout` through the gateway (gateway -> orders-service
-> orders-db three times, plus inventory-service) with a closed-loop load of N concurrent users for
6 seconds per level. The stack and the load generator share a 2-CPU cloud container, so absolute
numbers are low and noisy. Compare rows with each other, not with other machines.

## Results

| Version | req/s at 4 users | req/s at 32 users | p50 at 32 (ms) | p99 at 32 (ms) | errors at 32 |
| --- | --- | --- | --- | --- | --- |
| baseline | 279.3 | 234.2 | 28.6 | 2162.8 | 0.5% |
| + listen queue | 255.0 | 267.3 | 118.5 | 152.8 | 0.0% |
| + SQLite pool | 300.4 | 336.2 | 94.8 | 127.7 | 0.0% |
| + keep-alive, no Nagle | 510.9 | 498.2 | 61.2 | 124.8 | 0.0% |

Throughput at 4 users went from about 280 to about 510 requests/s (+83%). The 2.1 second p99 at
32 users, and the errors, are gone.

## What was wrong, in the order it was found

1. **Dropped connections under load (tail latency of exactly 1, 2 and 3 seconds).** Python's HTTP
   server listens with a queue of 5. At 32 users the kernel dropped 166 connection attempts
   (`ListenOverflows` in `/proc/net/netstat`, zero at 4 users), and each drop costs a one-second
   TCP retry. Fix: queue of 128. p99 at 32 users: 2163 ms -> 153 ms, errors 0.5% -> 0.
   *Note:* the median at 32 users went **up** (29 -> 119 ms). Before the fix, dropped connections
   waited out their retries, which silently held back the real concurrency; afterwards every
   request is accepted and waits its turn. The old median flattered the system.
2. **A new SQLite connection for every query** (three per checkout). orders-db was the hottest
   service at about 64% CPU. Fix: a small connection pool. Throughput at 32 users 267 -> 336
   requests/s, orders-db CPU per request fell from about 2.4 ms to about 1.6 ms.
3. **A new TCP connection for every call between services.** Fix: reuse connections (HTTP/1.1
   keep-alive), `target/httpc.py`. **This first made things much worse**: 136 ms median at one
   user instead of 6 ms. The cause was Nagle's algorithm meeting delayed ACKs: the server sent
   headers and body as two small writes and the second waited for an ACK that the client was
   delaying. Setting `TCP_NODELAY` fixed it. Throughput at 32 users 336 -> 498 requests/s. A run
   made while I was also running tests was discarded and redone, because it contaminates timing.

## Checks that the changes did not break anything

- 9 target tests pass, including new ones that the connection-reusing client raises the same
  errors as `urllib` (connection refused, HTTP 500, timeout) and really reuses one connection.
- Live detection re-run after all three changes: **5 of 5 faults detected, 0 false alarms on the
  healthy run, 0 extra incidents** (`perf/results/live-detect-after.json`).

## Follow-up: the pool kept every connection it ever opened

Found independently by a run on a second machine and confirmed in the saved data: the SQLite
pool from step 2 had no size limit, so orders-db memory grew with the busiest moment it had ever
seen (29 MB at 1 user, 73 MB at 32, 87 MB at 64, with keep-alive) and never shrank. That looks like
a leak. The pool is now capped (`DB_POOL_SIZE`, default 4). Pool sizes 2, 4 and 8 held orders-db at
38-44 MB at 32-64 users against 65-103 MB unbounded, and their throughput was within run-to-run
noise of the unbounded pool (one run per size, so small differences are not established).

A healthy system with a traffic surge halfway through (`python -m perf.surge_check`, 4 -> 28 users)
raised **no false alarm with either pool**, so the growth did not trip the detector in that test.
The risk is real but not demonstrated; the memory growth itself is.

## What this does not show

- The load generator runs on the same two CPUs, so it takes CPU from the stack. Above about 500
  requests/s the machine, not the code, is the limit. A separate load machine or a compiled tool
  (`hey`, `wrk`) is needed to find the stack's own ceiling.
- Two runs per row only show run-to-run noise (typically 5-10%); the larger changes here are well
  outside it, small ones would not be.
- Nothing here measures containers. `deploy.verify` now prints crash-recovery and rollback
  timings, and CI prints container CPU and memory, but there is no Docker in this container.
- The detector was only re-checked at the default traffic level. `perf.detect_bench` measures it at
  several traffic levels and has not been run yet.

## Next

Run the same suite on a real machine (`perf.bench`, `deploy.verify --detect`, `perf.detect_bench`),
look for the next bottleneck there, and only then move on to Phase B.
