# Security Policy

## Scope

`sre-reliability-labs` is a local-only engineering laboratory for reproducing container, Kubernetes, and distributed-systems failure modes on a developer workstation. It is **not** intended for internet-facing or production deployment.

## Local Security Boundaries

- **Loopback exposure only:** Lab services bind to `127.0.0.1` where applicable. Do not bind lab services or the shared observability stack to public interfaces or expose them on untrusted networks.
- **Grafana anonymous admin:** `shared/observability/docker-compose.yml` enables anonymous Grafana admin access strictly for ephemeral local convenience. Never copy those settings into shared or production deployments.
- **No secrets or production data:** Never commit `.env` files, private keys (`*.pem`, `*.key`), kubeconfigs, API tokens, employer code, or production customer data.
- **Container hardening:** Lab application containers run as non-root users with minimal base images, dropped Linux capabilities, and read-only root filesystems wherever compatible with the failure mode under study.

## Reporting a Vulnerability

If you discover a security issue in this repository (for example, an accidental credential leak, unsafe script execution, or vulnerable dependency):

1. **Do not** open a public issue containing sensitive details or leaked credentials.
2. Report privately via GitHub Security Advisories on this repository (`Security` -> `Advisories` -> `Report a vulnerability`).
3. Include affected file paths, reproduction steps, and suggested remediation.

Reports are reviewed on a best-effort basis and remediated promptly once verified.
