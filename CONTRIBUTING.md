# Contributing to open-serve

Thanks for your interest in contributing!

## Ways to contribute

- **Add a model preset** — the lowest-friction contribution. Presets live in `catalog/models/`; each one carries tested `vllmArgs`, GPU sizing per accelerator, and probe metadata. When a new open-weights model ships, a preset PR makes it deployable for everyone.
- **Report bugs / request features** via GitHub issues.
- **Improve docs** — quickstarts, runbooks, and cloud guides live in `docs/`.
- **Code** — the gateway, probe, and status services are small FastAPI apps; the chart is standard Helm.

## Development

- Python services use Python 3.11. Each service under `services/` has its own `requirements.txt` (and `requirements-dev.txt` where tests exist). Run tests with `pytest` from the service directory.
- Chart changes: `helm lint charts/open-serve` and `helm template` should both pass.
- CI must be green: lint, unit tests, `helm lint`, secret scanning, and the naming lint.

## Commit sign-off (DCO)

We use the [Developer Certificate of Origin](https://developercertificate.org/). Sign your commits with `git commit -s`.

## Code of conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).
