# FluxCD GitOps reference layout

A reference for deploying open-serve with FluxCD v2 using the classic
kustomize **base + environment overlay** pattern. Copy `environments/example`
and `clusters/example` per real environment (e.g. `dev`, `prod`) and adapt.

## Layout

```
deploy/flux/
  base/                        # shared, environment-agnostic definitions
    configs/                   # Namespace open-serve + ServiceAccount open-serve-worker
    kuberay-operator/          # upstream KubeRay chart (HelmRepository + HelmRelease)
    prometheus-operator-crds/  # Prometheus Operator CRDs (decoupled from the stack)
    monitoring/                # kube-prometheus-stack
    open-serve/                # the open-serve chart (OCIRepository + HelmRelease)
  environments/example/        # one overlay per environment
    configs/                   # + cloud IAM binding for the worker SA (commented examples)
    kuberay-operator/          # + values.yaml (replicas, resources)
    prometheus-operator-crds/
    monitoring/                # + values.yaml (retention, dashboards, alerting)
    open-serve/                # + values.yaml (gateway/monitoring/statusPage, serveModels)
  clusters/example/
    infrastructure.yaml        # Flux Kustomizations wiring the overlay dirs together
```

### The values-override pattern

Environment-specific Helm values live in each overlay's `values.yaml`,
bundled into a ConfigMap via kustomize `configMapGenerator` and referenced by
the HelmRelease through `spec.valuesFrom`. The `kustomizeconfig.yaml`
`nameReference` entry makes kustomize rewrite the HelmRelease's
`valuesFrom.name` to the generated ConfigMap name (which carries a content
hash suffix), so every values change produces a new ConfigMap name and
reliably triggers a Helm upgrade.

> **Serve-script ConfigMap changes require a pod restart.** Helm upgrades
> update the `serve-<model>-script` ConfigMaps, but running Ray pods keep
> the file contents they mounted at startup. After changing a serve script
> or engine params, delete the affected model's head and worker pods so
> they restart with the new ConfigMap.

## The dependsOn DAG

`clusters/example/infrastructure.yaml` declares one Flux Kustomization per
component, ordered by `dependsOn`:

```
configs ──┬─▶ kuberay-operator ──────────┬─▶ open-serve
          └─▶ prometheus-operator-crds ─▶ monitoring ─┘
```

- **configs** first: everything else deploys into the `open-serve` namespace
  it creates.
- **kuberay-operator** must be running before open-serve's RayService CRs
  can be reconciled.
- **prometheus-operator-crds** before **monitoring** and (transitively)
  before open-serve, whose chart ships PodMonitors and PrometheusRules.
- **open-serve** last, once both the operator and monitoring are ready.

## Chart sources

- `kuberay-operator` and `kube-prometheus-stack` pull from their **upstream**
  Helm repositories (`https://ray-project.github.io/kuberay-helm/`,
  `https://prometheus-community.github.io/helm-charts`).
- The `open-serve` chart pulls from an OCI registry
  (`oci://ghcr.io/hari-kathi/charts/open-serve`). If your registry uses
  ambient cloud credentials (e.g. GCP Artifact Registry with Workload
  Identity), uncomment `spec.provider: gcp` in
  `base/open-serve/repository.yaml`.

## Bootstrap

Fork/copy this layout into your GitOps repo, then:

```bash
flux bootstrap github \
  --owner=<you> \
  --repository=<your-gitops-repo> \
  --path=deploy/flux/clusters/example
```

Bootstrap generates `clusters/example/flux-system/` (gotk-components.yaml +
gotk-sync.yaml) in the repo — those files are deliberately **not** part of
this reference — and points the cluster at the repo. From then on, merging
to the tracked branch is the deployment mechanism.

Validate overlays locally before pushing:

```bash
kubectl kustomize deploy/flux/environments/example/open-serve
```

## Cloud-specific bits (all optional, all commented)

- **Worker SA → object storage**: `environments/example/configs/kustomization.yaml`
  has a commented GKE Workload Identity patch (plus EKS/AKS annotation
  pointers) for granting the `open-serve-worker` ServiceAccount read access
  to your model bucket.
- **Internal load balancer**: `environments/example/open-serve/internal-lb.yaml.example`
  shows a generic LoadBalancer Service with GKE/EKS/AKS internal-LB
  annotations commented as per-cloud examples.
