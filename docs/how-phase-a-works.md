# How Phase A works (no AI involved)

Phase A is a real four-service stack, a statistical detector and a scripted checklist. No model is
called anywhere in it. This page explains each part and where it lives in the code.

## Where the numbers come from

1. **Each service counts its own requests** (`target/common.py`, class `Metrics`). For every request
   it records how long it took and whether it succeeded. It keeps only the last few seconds.
2. **Each service reports a summary on `/metrics`** in Prometheus text format: error rate (failed
   requests / total x 100), p95 latency, resident memory read from `/proc/self/statm`, CPU and
   request rate.
3. **The monitor scrapes every service on a timer** (`Stack.scrape` in `target/stack.py`) and keeps
   one list of readings per service and metric.
4. **A service that does not answer within 0.4 s is recorded as 100% errors**, the same signal an
   "up == 0" alert would give.

Logs are separate. Each service writes JSON lines to its own file. The detector never reads them;
they are used afterwards to find the cause.

## The detector (`monitor/detect.py`)

For each service and metric:

1. **Learn normal** from the first `train` readings (30 in the live setup): the median, and a
   spread of 1.4826 x the median absolute deviation, never smaller than a per-metric floor.
2. **A reading is abnormal only if all three hold:**
   - it is at least `min_delta` above normal (ignores tiny wobbles);
   - it is at least 6 spreads above normal (ignores ordinary noise);
   - it is at least `min_ratio` times normal (used for latency).
3. **It must persist.** `consecutive` abnormal readings in a row (4 in the live setup) are needed;
   one spike is ignored.
4. **One alert is raised**, stating the value and what normal looked like. After 10 calm readings
   the detector re-arms.
5. **Alerts are grouped into incidents** (30-minute gap, with the user-facing service as the
   anchor), and deploys or config changes in the 30 minutes before onset are listed as suspects.

Worked example: the gateway error rate is normally 0%, so the learned spread is the floor, 0.5. The
rule needs at least +3 points and 6 spreads, so an error rate of 3% or more for four readings in a
row raises an alert. A bad release at 10-18% errors fires within a few readings; a healthy run
does not fire.

## The checklist (`python -m deploy.verify`)

Twelve pass/fail checks run against the running containers:

| Check | Condition |
| --- | --- |
| all services healthy | every service's health endpoint answers (4/4) |
| users can check out | 40 requests, 0% errors |
| bad release is visible to users | more than 5% errors after deploying the buggy release |
| detector raises an alert | the detector rule above produces a new alert |
| the bug is findable in the logs | `AttributeError` appears in the orders-service logs |
| gateway reads container logs | the command gateway returns the log search result |
| gateway refuses a destructive command | `rm -rf /` is denied by the allowlist |
| gateway holds a rollback for approval | the command is queued as `needs_approval`, not run |
| approved rollback restores service | 0% errors after the approved rollback |
| a crash is visible to users | the gateway returns errors while orders-service is down |
| crashed service is restarted by policy | the container restart policy brings it back; time is measured |
| stack left healthy | 0% errors at the end |

## How the answer is known

Faults are injected by the test harness at a known moment, so the true cause is known before the
detector or any investigator sees the data. Scoring is plain code comparing what was injected with
what was reported: whether it fired, how many readings after the fault, and whether anything fired
on a healthy run.

## Limits

- The stack is the four demo services. Pointing it at another project needs that project to expose
  `/metrics` and logs, and the service list in `target/` and the gateway allowlist to become
  configurable. That is not built.
- Latency detection is marginal for the slow-query fault at low load, because the latency rule
  needs +40 ms.
- A 16x traffic surge can trigger a latency alert, since the baseline is not load-aware.
