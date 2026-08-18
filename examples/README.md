# Examples

- [`kind-local/`](kind-local/) — CPU-only demo on a local kind cluster: builds the service images, installs upstream KubeRay, deploys the chart with a tiny `custom`-runner echo model, and asserts auth + routing through the gateway end to end. Run `kind-local/run.sh`; tear down with `kind-local/cleanup.sh`. No GPUs required.
- [`gcp-quickstart/`](gcp-quickstart/) — reference Terraform root (bootstrap → network → cluster with GPU node pools) taking a fresh GCP project to a serving-ready GKE cluster; pair with [`deploy/flux/`](../deploy/flux/) for the software layer. See the directory README for GPU-quota prerequisites.
