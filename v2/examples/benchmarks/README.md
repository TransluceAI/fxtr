# Fxtr2 Benchmarks

This directory contains benchmarks for measuring performance of various fxtr2 components.

## store_benchmarks/

Benchmarks for measuring entity transfer performance between scopes and namespaces.

### Usage

```bash
# List available cases (from v2 directory)
uv run python -m examples.benchmarks.store_benchmarks list

# Run all benchmarks
uv run python -m examples.benchmarks.store_benchmarks run

# Filter by keyword (substring match on case name)
uv run python -m examples.benchmarks.store_benchmarks run -k pin
uv run python -m examples.benchmarks.store_benchmarks run -k tree

# Customize iterations
uv run python -m examples.benchmarks.store_benchmarks run -n 3

# Save results to JSON
uv run python -m examples.benchmarks.store_benchmarks run --json results.json

# Trace cases with viztracer (requires `uv sync --group prof`)
uv run python -m examples.benchmarks.store_benchmarks trace -k tree_pin
uv run python -m examples.benchmarks.store_benchmarks trace -k pin -o my_traces/
```

### Benchmark Cases

| Name | Entity shape | Transfer | Pins | Entities |
|------|-------------|----------|------|----------|
| `tree_pin` | Depth-10 binary tree, 1KB payload | Memory → SQLite | 1 | 2047 |
| `tree_retrieve` | Depth-10 binary tree, 1KB payload | SQLite → Memory | 1 | 2047 |
| `flat_pin` | 500 independent flat entities, 1KB | Memory → SQLite | 500 | 500 |
| `flat_retrieve` | 500 independent flat entities, 1KB | SQLite → Memory | 500 | 500 |
| `flat_indexed` | 500 entities with 20 indexable fields, 1KB | Memory → SQLite + 20 indexers | 500 | 500 |

### CLI Reference

```
uv run python -m examples.benchmarks.store_benchmarks COMMAND [OPTIONS]
```

| Command | Options | Description |
|---------|---------|-------------|
| `run` | `-k PAT`, `-n NUM`, `--json FILE` | Run benchmarks |
| `trace` | `-k PAT`, `-o DIR` | Trace cases with viztracer (default dir: `./traces/`) |
| `list` | `-k PAT` | List available cases |

All commands accept `-k PATTERN` to filter cases by substring match on name.
