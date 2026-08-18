# Reference infrastructure (optional)

Terraform modules for standing up the underlying infrastructure. **Bring-your-own-cluster is a first-class path** — you only need these if you want a turnkey environment.

Layout:

```
terraform/gcp/
  bootstrap/   # enable APIs on an existing project, optional state + model buckets
  network/     # VPC, GKE-ready subnet, Cloud NAT
  cluster/     # GKE + GPU node pools (scale-to-zero, spot, multi-SKU via a pool map)
```

See [`gcp/README.md`](gcp/README.md) for module inputs and
[`examples/gcp-quickstart/`](../examples/gcp-quickstart/) for a wired-up root module.
AWS and Azure follow after GCP.
