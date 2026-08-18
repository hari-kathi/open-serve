# Security Policy

## Reporting a vulnerability

Please report vulnerabilities privately via **GitHub → Security → Report a vulnerability** on this repository. Do not open a public issue for security reports.

You can expect an acknowledgment within a few days. Please include reproduction steps and the affected component (gateway, probe, status, chart, runtime).

## Scope notes

- The gateway is the authentication boundary; Ray Serve services behind it are unauthenticated by design and must not be exposed directly.
- API keys are provisioned as Kubernetes Secrets; never commit keys to git. CI runs secret scanning on every PR.

## Supported versions

Pre-1.0: only the latest release receives security fixes.
