rosdistro
=========

Tools to work with catkinized rosdistro files.

## Profiling Cache Builds

`rosdistro_build_cache` includes optional OpenTelemetry-backed profiling and tracing to break down where time is spent across phases, distributions, resource types, fetch mechanisms (including cache hits and `local_neighbor` reuse), provider sub-resources, and underlying I/O operations (`http:get`, `http:rate_limit_sleep`, `git:*`, `tar:extract`, `sanitize:*`, `yaml:*`, `gzip:*`).

### Installation

Profiling dependencies are optional so normal `rosdistro` usage stays lightweight. Install the `profile` extra to enable profiling:

```bash
pip install 'rosdistro[profile]'
# or, in an editable checkout:
pip install -e '.[profile]'
```

### Running `rosdistro_build_cache` with Profiling

Print a timing summary table at the end of a cache build:

```bash
rosdistro_build_cache index-v4.yaml rolling --extended --profile
```

Write all profiling artifacts (`profile-summary.txt`, `profile-stacks.folded`, and `profile-trace.json`) into a directory:

```bash
rosdistro_build_cache index-v4.yaml rolling --extended --profile-dir ./profile_out
```

You can also request individual output files or enable profiling via environment variables (useful in CI / GitHub Actions):

| Flag / Environment Variable | Description |
|---|---|
| `--profile` / `ROSDISTRO_PROFILE=1` | Enable profiling and print the timing summary tables to stdout |
| `--profile-dir DIR` / `ROSDISTRO_PROFILE_DIR=DIR` | Write `profile-summary.txt`, `profile-stacks.folded`, and `profile-trace.json` into `DIR` (implies `--profile`) |
| `--profile-folded PATH` | Write Brendan Gregg folded stacks to `PATH` (implies `--profile`) |
| `--profile-json PATH` | Write Chrome Trace Event JSON + structured `rosdistroProfile` summary to `PATH` (implies `--profile`) |

Profiling summaries and artifacts are written in a `finally` block, so partial results are still captured if a build exits with an error.

### Viewing Flame Graphs and Traces

- **Interactive flame graph ([Speedscope](https://www.speedscope.app/))**: Open either `profile-stacks.folded` (aggregated Left Heavy / Sandwich views) or `profile-trace.json` (adds chronological Time Order view).
- **Interactive span timeline ([Perfetto UI](https://ui.perfetto.dev/))**: Open `profile-trace.json` to inspect individual spans chronologically along with their `resource_type`, `fetch_type`, `target`, `distro`, `status`, and `detail` metadata.
- **Static SVG flame graph**: Convert `profile-stacks.folded` with [FlameGraph](https://github.com/brendangregg/FlameGraph) or [inferno](https://github.com/jonhoo/inferno):

  ```bash
  flamegraph.pl ./profile_out/profile-stacks.folded > flamegraph.svg
  # or:
  inferno-flamegraph < ./profile_out/profile-stacks.folded > flamegraph.svg
  ```

### Exporting to OpenTelemetry Backends

Because spans are emitted through `opentelemetry.trace` with `rosdistro.*` attributes (`rosdistro.kind`, `rosdistro.resource_type`, `rosdistro.fetch_type`, `rosdistro.target`, `rosdistro.distro`, `rosdistro.status`, `rosdistro.detail`), you can also send traces to OTLP-compatible backends (Jaeger, Grafana Tempo, etc.):

- Run `rosdistro_build_cache` under `opentelemetry-instrument` (with `OTEL_EXPORTER_OTLP_ENDPOINT` configured), or
- Pass your own `opentelemetry.sdk.trace.TracerProvider` into `CacheProfiler(tracer_provider=...)` (or attach `RosdistroSpanProcessor` directly to an existing provider).
