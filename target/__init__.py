"""A small REAL application: separate processes, real HTTP, a real SQL database, real logs.

Unlike `shop/` (which computes telemetry from a model), nothing here is computed on behalf of the
monitor. Faults are real events: a process is killed, an index is dropped, memory is actually
allocated, a code path actually raises. The monitor reads what the processes really report.
"""
