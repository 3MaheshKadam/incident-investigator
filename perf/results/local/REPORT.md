# Local performance report

All numbers below come from files in this folder. Nothing is estimated.

## Environment

From `environment.txt`:

- Ubuntu 26.04 LTS, native Linux (not WSL2), kernel 7.0.0-28-generic
- Intel Core i5-11320H, 4 cores / 8 threads, 400–4500 MHz, `intel_pstate`
- 30 GiB RAM, 8 GiB swap
- Python 3.14.4 (host), Docker 29.6.2, Compose v5.3.1; Docker sees NCPU=8 and 31.4 GiB, cgroup v2
- Containers use `python:3.11-slim`, `mem_limit: 256m` and `pids_limit: 128`, with no CPU limit

Run conditions:

- **Power.** The power profile was switched from `balanced` to `performance` before Part A. The laptop was on AC for Part A (22:08–22:15) and for the pool comparison (23:27–23:30). It was **on battery** for a period between 23:01 and 23:25; the exact start is unknown. The three runs made in that period were slower by up to 13% on unchanged code. They are kept as `BATTERY-INVALID-*.json` and not used anywhere in this report.
- **Background load.** Chrome (25–32 processes) and VS Code stayed open during every run.
- **Load generator.** `perf.bench` drives the services from Python threads on the same machine. Its own CPU use is not recorded.

Code versions:

| Label | Commit | What |
|---|---|---|
| baseline | `a71e0f4` | before Phase A, plus the `perf/` scripts copied in |
| optimized / unbounded pool | `ccf03a5` | Phase A branch with an unbounded `orders-db` pool |
| capped pool | `38aecdc` (main after #15) | `orders-db` pool capped at `DB_POOL_SIZE=4` |

## A. Baseline vs optimized (`perf.bench`, 6 s per level, 200k rows)

Values are means of two runs. Spread is |run1 − run2| / mean, for req/s. Latency is measured at the client, in ms.

| Concurrency | Baseline req/s (spread) | Optimized req/s (spread) | Change | Baseline p50 / p95 / p99 | Optimized p50 / p95 / p99 | Error % (base / opt) |
|---|---|---|---|---|---|---|
| 1 | 385 (1.4%) | 784 (2.9%) | +104% | 2.5 / 3.0 / 3.3 | 1.2 / 1.4 / 1.7 | 0 / 0 |
| 2 | 566 (1.1%) | 1260 (1.1%) | +123% | 3.5 / 4.0 / 4.5 | 1.6 / 1.9 / 2.2 | 0 / 0 |
| 4 | 835 (1.8%) | 1816 (0.2%) | +118% | 4.8 / 6.0 / 6.5 | 2.1 / 2.8 / 3.1 | 0 / 0 |
| 8 | 969 (0.9%) | 1912 (0.1%) | +97% | 8.2 / 10.4 / 11.4 | 4.1 / 5.4 / 6.1 | 0 / 0 |
| 16 | 874 (4.2%) | 1886 (0.1%) | +116% | 11.7 / 14.9 / 17.2 | 8.3 / 11.8 / 13.8 | 0 / 0 |
| 32 | 848 (0.7%) | 1790 (0.4%) | +111% | 12.4 / 16.6 / **1036.8** | 17.3 / 26.4 / 30.9 | 0.025 / 0 |

- **Throughput.** The optimized code carries 1.97–2.23× the baseline's traffic at every level. The largest spread between runs was 4.2%.
- **Baseline tail latency.** The baseline p99 at concurrency 32 is about 1 s, and its max latency was 3004 ms and 2307 ms in the two runs. The optimized max at that level was 43.5 ms and 43.1 ms.
- **Higher p50 at concurrency 32.** The optimized p50 at 32 (17.3 ms) is higher than the baseline's (12.4 ms), because it completes 2.1× as many requests with the same 32 clients.

### Where CPU goes (mean of two runs; % of one core)

| Concurrency | Baseline: gw / orders / inv / db (sum) | Optimized: gw / orders / inv / db (sum) | CPU ms per request (base → opt) |
|---|---|---|---|
| 1 | 25 / 32 / 11 / 41 (109) | 35 / 33 / 7 / 24 (99) | 2.82 → 1.26 |
| 4 | 64 / 89 / 24 / 110 (287) | 101 / 105 / 19 / 73 (298) | 3.44 → 1.64 |
| 8 | 82 / 119 / 31 / 163 (394) | 120 / 128 / 22 / 86 (355) | 4.07 → 1.86 |
| 16 | 76 / 113 / 28 / 163 (380) | 124 / 130 / 22 / 89 (366) | 4.35 → 1.94 |
| 32 | 73 / 110 / 27 / 159 (370) | 121 / 129 / 21 / 93 (364) | 4.37 → 2.03 |

- **Total service CPU per request** is 55–57% lower in the optimized code.
- **The biggest consumer moved.** In the baseline, `orders-db` used the most CPU (159–163% from concurrency 8 up). In the optimized code it uses 86–93%, while api-gateway and orders-service level off at 120–130% each from concurrency 8. Throughput levels off at the same point.
- **Each service's own p95** (optimized, concurrency 32) is gateway 22.7 ms, orders-service 17.1 ms, orders-db 0.5 ms, and inventory 0.0 ms. These measure handler time only. `/stock` returns a constant, so inventory rounds to 0.0.

### Pool cap: unbounded vs capped (interleaved runs, all on AC)

Runs were alternated (unbounded-1, capped-1, unbounded-2, capped-2) so that any drift affects both equally. Files: `local-pool-{unbounded,capped}-{1,2}.json`.

| Concurrency | req/s unbounded | req/s capped | Change | orders-db RSS unbounded (MB) | orders-db RSS capped (MB) |
|---|---|---|---|---|---|
| 1 | 778 | 777 | −0.1% | 33.8, 33.9 | 34.0, 33.9 |
| 8 | 1901 | 1889 | −0.6% | 46.2, 47.2 | 45.6, 45.1 |
| 16 | 1876 | 1859 | −0.9% | 58.7, 54.5 | 54.1, 54.0 |
| 32 | 1775 | 1742 | −1.9% | 72.9, 83.6 | 63.6, 62.9 |

The cap lowers `orders-db` memory at concurrency 32 by 9–21 MB and makes it repeatable: the two capped runs differ by 0.7 MB, the two unbounded runs by 10.7 MB. The throughput cost is at most 1.9%.

## B. Containers (`docker compose --profile load`, 200k rows)

From `verify.txt`, which records 12/12 checks passed:

| Timing | Seconds |
|---|---|
| crash → recovery (restart policy) | 0.8 |
| approve rollback → healthy | 15.7 |
| bad release → alert | 17.0 |

Container CPU and memory come from `docker stats --no-stream` (`docker-stats.txt`), sampled twice, with traffic from **`target.loadgen` run on the host** at 4 threads against 127.0.0.1:8080. The in-container loadgen does not start; see anomaly 1.

| Container | CPU % (sample 1 / 2) | Memory (MiB, limit 256) | PIDs |
|---|---|---|---|
| api-gateway | 20.6 / 20.1 | 18.3 / 18.6 | 1 |
| orders-service | 2.1 / 2.4 | 18.2 / 18.7 | 5 / 4 |
| inventory-service | 7.8 / 0.6 | 18.3 / 18.4 | 5 / 4 |
| orders-db | 1.7 / 4.0 | 22.1 / 22.3 | 5 / 4 |

At idle (`docker-stats-idle.txt`, no traffic) every container used 16–18 MiB, and all except orders-db used ≤0.02% CPU. `deploy.watch --seconds 60` raised no alerts with or without traffic (`watch.txt`, `watch-idle.txt`).

## C. Detection on the real stack

### Default load, both variants (`target.run`, 150 samples, 300k rows, unbounded pool) — `live-detect.json`

| Fault | Variant 0: detected, ttd | Variant 1: detected, ttd | Alert |
|---|---|---|---|
| bad_deploy | yes, 3 | yes, 3 | gateway error rate 10% |
| memory_leak | yes, −50 | yes, −54 | inventory memory 62–63 MB (normally 26) |
| slow_query | yes, 29 | yes, 7 | gateway p95 44–48 ms (normally 2) |
| feature_flag | yes, 6 | yes, 6 | gateway error rate 9% |
| dependency_outage | yes, 3 | yes, 3 | gateway error rate 38–40% |
| healthy ×2 | 0 false alarms | | |

10/10 faults were detected, with 0 extra incidents. ttd is in samples (0.5 s each) and counts from the start of user impact. A negative value means the alert fired before users were affected: for memory_leak, impact is the first OOM.

### Detection vs load (`perf.detect_bench`, variant 0, 100 samples) — `local-detect-vs-load.json` (unbounded pool) and `local-detect-load16-capped.json` (capped pool)

| Load threads | bad_deploy | memory_leak | slow_query | feature_flag | dependency_outage | healthy false alarms |
|---|---|---|---|---|---|---|
| 1 | ttd 4 | detected, ttd n/a | **missed** | ttd 6 | ttd 3 | 0 |
| 4 | ttd 3 | detected, ttd n/a | **missed** | ttd 6 | ttd 3 | 0 |
| 16 | ttd 3 | detected, ttd n/a | ttd 4 | ttd 6 | ttd 3 | 0 |
| 16, capped pool | ttd 3 | detected, ttd n/a | ttd 5 | ttd 6 | ttd 3 | 0 |

**Pool version.** Part C (both tables above, except the last row) ran on the **old unbounded pool** (`ccf03a5`). The capped row ran on main `38aecdc`. At load 16 the two pools give the same detection result.

## What looks wrong or surprising

1. **The in-container loadgen crash-loops**, on both `a71e0f4` and `38aecdc`. The error is `ModuleNotFoundError: No module named 'monitor'`. `target/loadgen.py` imports `target.episode`, which imports `monitor.detect`, but `Dockerfile.stack` copies only `target/`. As a result `docker compose --profile load` produces no traffic, and the Part B numbers above needed host-driven load instead.
2. **The baseline had a 1-second tail** at concurrency 32: p99 1036.8 ms, max 3004 ms. That is the TCP retry delay when the listen backlog is full. The optimized code (`request_queue_size = 128`) does not show it: max 43 ms.
3. **orders-db memory grows with peak concurrency when the pool is unbounded.** RSS was 34 MB at concurrency 1, then 81 and 91 MB at concurrency 32 in Part A, and 73 and 84 MB in the pool runs. Each concurrent query opened a new SQLite connection and kept it forever. The cap removes this (63 MB, stable).
4. **slow_query detection is marginal at low load.** The live latency rule needs p95 to rise by at least 40 ms (`target/episode.py:38`). Every slow_query alert fired at 44–60 ms. At load 4 the same variant was detected once (Part C, ttd 29) and missed once (`detect_bench`). The fault was injected at sample 55 and detected at sample 84 in Part C, so the 100-sample window in `detect_bench` is not what caused the miss.
5. **memory_leak's ttd is n/a in `detect_bench`, and this comes from the benchmark setup, not the detector.** `detect_bench` records 100 samples by default, while `target.run` records 150. The leak starts at sample 42 at 3.5 MB/s, so it does not reach the 150 MB OOM point within 100 samples. With no OOM there is no impact time to measure ttd from. The memory alert fired in every run.
6. **Throughput falls past concurrency 8**: 1912 req/s at 8, then 1790 at 32 (optimized). Total service CPU is about 360% on an 8-thread machine, so the host as a whole is not saturated when throughput stops rising.
7. **The gateway container used about 20% CPU under light host load**, more than any other container. Two single `docker stats` snapshots are not enough to explain it, and the request rate during the snapshots was not recorded.
8. **The bench clients ran on battery for part of the session**, which slowed unchanged code by up to 13%. The affected runs are excluded and marked `BATTERY-INVALID`. One `detect_bench` attempt was aborted because the code was rebased while it was running; its partial log is kept as `detect-vs-load-ABORTED-mixed-code.log`.

## Top 3 bottlenecks, ranked by evidence

1. **api-gateway and orders-service CPU.** From concurrency 8 up, each levels off at 120–130% of a core, at the same point where throughput stops rising (Part A CPU table). These are single Python processes, and 120–130% matches one core of interpreter work plus kernel time on other threads. Neither process can use more cores.
2. **orders-service's three sequential DB calls per checkout.** Each `/checkout` makes 3 sequential `orders-db` queries inside orders-service (`target/services.py`, `order()`). orders-service has the highest CPU of any service, and its own p95 (17.1 ms at concurrency 32) is most of the gateway's (22.7 ms).
3. **Not measured: the load generator.** `perf.bench` runs its client threads in Python on the same 8 threads as the four services, and its CPU use is not recorded. It may be capping throughput. Part 2 should rule this out first, with a separate client such as `hey` or `wrk` (neither is installed yet).

orders-db is not a bottleneck in the optimized code: it uses 86–93% CPU and its own p95 is 0.5 ms.

## What these numbers do not prove

- **They don't predict production capacity.** The services, the load generator, Chrome and VS Code all shared one laptop, so the absolute req/s values describe this machine under these conditions only.
- **Each `detect_bench` cell is one run.** A single detected or missed result per load level cannot separate the effect of load from run-to-run noise. This matters most for slow_query.
- **The Part B container numbers are not under realistic load.** They come from two `docker stats` snapshots under about 4 light host threads, not a sustained or measured load, and the in-container load path was broken.
- **Parts A and C use only the unbounded-pool code.** Only the pool comparison and the load-16 capped row ran on the current main.
- **Nothing here measures the LLM investigator.** No model was called.
