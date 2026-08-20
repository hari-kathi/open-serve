# Choosing your path

Three ways to bring up open-serve, ordered by commitment. All three end at the same place: an OpenAI-compatible endpoint behind API-key auth, serving models you enable with one line of values.

| Path | Needs | Time to first request | Approx. cost | Get |
|---|---|---|---|---|
| **[Local on kind](kind.md)** | Docker, kind, kubectl, helm | ~10 min | free | The full stack (gateway auth, routing, RayService) with a CPU demo model — no cloud account, no GPUs |
| **[GCP / GKE](gcp.md)** | GCP project with billing, OpenTofu/Terraform, gcloud | ~45 min + quota approval | ~$5–6/day idle; +~$0.20–0.70/hr per L4 GPU (spot/on-demand) | Production-shaped cluster with scale-to-zero GPU pools |
| **[AWS / EKS](aws.md)** | AWS account, OpenTofu/Terraform, aws CLI | ~45 min + quota approval | ~$6–7/day idle; +~$0.25–0.80/hr per L4 GPU (spot/on-demand) | Same, on EKS with IRSA |

**Start with kind** even if you're headed for a cloud — it proves the moving parts in ten minutes and the cloud guides assume you've seen them once.

!!! warning "The universal gotcha: GPU quota"
    Both clouds ship new accounts with **zero GPU quota**, hidden behind quota names you'd never guess (`GPUs (all regions)` on GCP; per-family vCPU quotas like `All G and VT Spot Instance Requests` on AWS). Both preflight scripts check these and tell you exactly what to request. **File quota requests before anything else** — approval is the only step on someone else's clock, and young accounts routinely get routed to manual review.

Everything else (cluster, gateway, CPU models, the full request path) works without GPU quota, so you can build and validate while requests are pending.
