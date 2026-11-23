# CLAUDE.md — Repository Guide for `sre-reliability-labs`

This file provides repository architecture, engineering standards, and verification rules for Claude Code and other coding agents working in `sre-reliability-labs`.

## Architecture

- `shared/observability/` — Shared Prometheus (`:9090`, `2s` scrape, `file_sd_configs` watching `prometheus/targets/*.json`) and Grafana (`:3000`, pre-provisioned RED + container dashboard) on named bridge network `sre-lab-net`.
- `shared/loadgen/` — Python 3.11+ (`uv`, `httpx`, `typer`, `rich`) async HTTP load generator producing latency percentiles, error breakdowns, and JSON evidence reports.
- `labs/<NN>-<slug>/` — Self-contained failure-mode labs attaching to `sre-lab-net` (Docker Compose) or running on local `kind`, each exposing a `broken` vs `fixed` profile and a per-lab `Makefile`.

## Experimental Standard

Every lab must follow the 7-step scientific workflow comparing `broken` vs `fixed` under identical synthetic load:

```text
OBSERVATION -> HYPOTHESIS -> TEST -> EVIDENCE -> ROOT CAUSE -> FIX -> PREVENTION
```

- **Never fabricate measurements.** Keep placeholders explicitly marked until live runs produce real telemetry and JSON reports.
- Write concisely like a senior SRE: specific, quantitative, zero marketing language.

## Security & Clean-Room Boundaries

- Clean-room personal code only. Zero employer references, proprietary code, internal hostnames, or credentials.
- Never commit `.env`, `.env.*`, `*.pem`, `*.key`, `kubeconfig*`, or dynamic target/evidence JSON artifacts.
- Bind lab application ports to loopback (`127.0.0.1:<port>:...`).
- Run lab application containers as non-root with minimal images, `no-new-privileges`, and dropped capabilities unless a lab explicitly requires a specific kernel capability (documented in that lab's README).
- Keep Docker configs 100% portable across Linux/macOS runtimes (no hardcoded OrbStack/Docker Desktop paths).

## Verification Requirements

Before finalizing changes:

1. `shared/loadgen`: run `uv run ruff check .` and `uv run pytest -v`.
2. Lab Python code: run `ruff check` where applicable.
3. Compose manifests: run `docker compose ... config -q` (include all `--profile` flags for profiled compose files).
4. Before marking any lab `Complete` in the root `README.md`, execute live `broken` vs `fixed` runs and record measured outputs in that lab's `README.md`.

## Commit Hygiene

- Use concise, descriptive commit messages scoped to a single cohesive change (e.g., `feat(lab01): add cgroup CPU throttling lab`).
- Verify `git status` is clean of `.venv/`, `tmp/`, and generated `prometheus/targets/*.json` files before committing.
