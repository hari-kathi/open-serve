# Reference infrastructure (optional)

Terraform modules for standing up the underlying infrastructure. **Bring-your-own-cluster is a first-class path** — you only need these if you want a turnkey environment.

Planned layout:

```
terraform/gcp/
  bootstrap/   # project, APIs, minimal service accounts, state bucket
  network/     # VPC, subnets, Cloud NAT
  cluster/     # GKE + GPU node pools (scale-to-zero, multi-SKU tiering)
```

Status: extraction and generalization in progress (pool map refactor, spot support, budget alerts). AWS and Azure follow after GCP.
