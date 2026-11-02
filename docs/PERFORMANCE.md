# Reproducible offline performance measurements

Run in the locked development environment:

```sh
python tools/benchmark_offline.py --samples 100 --history-rows 10000
```

The program creates and removes its own temporary directory and SQLite database.
It changes no installation state. Socket connection, UDP send and DNS resolution
are blocked during the workload. No application server or background scheduler
starts. The fixture contains one synthetic device, six preset slots, 100 stations
and the requested number of generated listening-history rows.

Measurements cover the real cached-preset, station and history router handlers
plus JSON serialization, and the actual schema inspection function. Each workload
gets five warmups; the report contains median and nearest-rank p95 milliseconds.
The empty database migration is a separately labelled single sample. Peak RSS is
the whole benchmark process (Linux KiB), not idle application memory.

These numbers do **not** measure HTTP middleware, dashboard rendering, full service
startup, Pi memory under playback, multiroom timing or radio performance. Those
remain explicit additional measurements. A fast synthetic benchmark is not a
performance certification for the Raspberry Pi.

For comparisons, use the same host, interpreter, locked dependencies, fixture size,
sample count and background load. Save the JSON privately with its exact source
fingerprint. Repeat an apparent regression before acting; changes above 10% in a
comparable critical workload require investigation rather than a silently weakened
threshold. Do not compare a laptop result against a Pi or a loaded CI runner.

The offline test verifies the harness's isolation and output contract. It does not
assert arbitrary timing thresholds on shared CI hardware. No optimization is
justified solely by these timings without identifying the actual slow path.
