# Releasing

A release is a single action:

```bash
git tag -a vX.Y.Z -m "open-serve vX.Y.Z: <one-line summary>"
git push origin vX.Y.Z
```

Everything downstream is automatic:

| Artifact / surface | How it gets the version |
|---|---|
| Service + runtime images on GHCR | Release workflow builds and tags `X.Y.Z` + `latest` |
| Helm chart on GHCR | Release workflow stamps `version`/`appVersion` in `Chart.yaml` at package time |
| Image tags *inside* the chart | Values default to `""` → templates fall back to `.Chart.AppVersion`, so the published chart always references its own release's images |
| Catalog presets | Carry no image pins — they inherit the chart default |
| Docs site version banner / examples | `docs/macros.py` reads the latest git tag at build time |
| README | Links to `releases/latest`; carries no version literal |
| Flux reference (`deploy/flux`) | Tracks charts via a semver range (users pin exact versions for prod) |
| GitHub Release | Created by the workflow with generated notes |

The in-repo `charts/open-serve/Chart.yaml` version is a development placeholder;
the release workflow overrides it in the published artifact. Bumping it in git
after a release is optional housekeeping, not required for correctness.

Pre-1.0, minor versions may include breaking changes — say so in the tag
message and let the generated release notes carry the detail.

Before tagging, make sure `main` is green (CI runs the full validation:
tests, chart lint + schema, catalog rendering, Terraform validate, docs build).
