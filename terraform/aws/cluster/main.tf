# EKS cluster for GPU model serving:
#   - one system managed node group for cluster services (gateway, controllers,
#     KubeRay operator)
#   - N GPU node groups driven by the `gpu_pools` map, each tainted so only
#     GPU workloads land on them, scale-to-zero via the cluster autoscaler
#   - OIDC provider + IRSA roles for the serving workers (model-store access)
#     and the cluster autoscaler

data "aws_region" "current" {}

# --- Cluster IAM role ---

data "aws_iam_policy_document" "cluster_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["eks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "cluster" {
  name               = "${var.name}-cluster"
  assume_role_policy = data.aws_iam_policy_document.cluster_assume.json

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "cluster" {
  for_each = toset([
    "AmazonEKSClusterPolicy",
    "AmazonEKSVPCResourceController",
  ])

  role       = aws_iam_role.cluster.name
  policy_arn = "arn:aws:iam::aws:policy/${each.value}"
}

# --- Cluster ---

resource "aws_eks_cluster" "this" {
  name     = var.name
  role_arn = aws_iam_role.cluster.arn
  version  = var.kubernetes_version

  enabled_cluster_log_types = var.enabled_log_types

  vpc_config {
    subnet_ids              = var.subnet_ids
    endpoint_public_access  = var.endpoint_public_access
    endpoint_private_access = var.endpoint_private_access
    public_access_cidrs     = var.public_access_cidrs
  }

  tags = var.tags

  depends_on = [aws_iam_role_policy_attachment.cluster]
}

# --- OIDC provider (the IRSA foundation) ---
#
# Registers the cluster's OIDC issuer with IAM so Kubernetes service accounts
# can assume IAM roles via sts:AssumeRoleWithWebIdentity (IRSA).

data "tls_certificate" "oidc" {
  url = aws_eks_cluster.this.identity[0].oidc[0].issuer
}

resource "aws_iam_openid_connect_provider" "this" {
  url             = aws_eks_cluster.this.identity[0].oidc[0].issuer
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.oidc.certificates[0].sha1_fingerprint]

  tags = var.tags
}

locals {
  oidc_issuer_host = trimprefix(aws_eks_cluster.this.identity[0].oidc[0].issuer, "https://")
}

# --- Node IAM role (shared by the system and GPU node groups) ---

data "aws_iam_policy_document" "node_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "node" {
  name               = "${var.name}-node"
  assume_role_policy = data.aws_iam_policy_document.node_assume.json

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "node" {
  for_each = toset([
    "AmazonEKSWorkerNodePolicy",
    "AmazonEC2ContainerRegistryReadOnly",
    "AmazonEKS_CNI_Policy",
  ])

  role       = aws_iam_role.node.name
  policy_arn = "arn:aws:iam::aws:policy/${each.value}"
}

# --- System node group ---

resource "aws_eks_node_group" "system" {
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "system"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = var.subnet_ids

  instance_types = [var.system_instance_type]
  ami_type       = "AL2023_x86_64_STANDARD"
  disk_size      = var.system_disk_size_gb

  scaling_config {
    min_size     = var.system_min_nodes
    max_size     = var.system_max_nodes
    desired_size = var.system_desired_nodes
  }

  labels = {
    workload-type = "system"
  }

  tags = var.tags

  lifecycle {
    # The cluster autoscaler owns desired_size after creation.
    ignore_changes = [scaling_config[0].desired_size]
  }

  depends_on = [aws_iam_role_policy_attachment.node]
}

# --- GPU node groups (data-driven) ---
#
# AL2023_x86_64_NVIDIA ships the NVIDIA drivers baked into the AMI; only the
# device plugin (installed in the software layer) is needed on top. The taint
# keeps non-GPU workloads off the expensive nodes, and the autoscaler
# discovery tags let the cluster autoscaler scale the pools from zero.

resource "aws_eks_node_group" "gpu" {
  for_each = var.gpu_pools

  cluster_name    = aws_eks_cluster.this.name
  node_group_name = each.key
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = coalesce(each.value.subnet_ids, var.subnet_ids)

  instance_types = [each.value.instance_type]
  ami_type       = "AL2023_x86_64_NVIDIA"
  capacity_type  = each.value.spot ? "SPOT" : "ON_DEMAND"
  disk_size      = each.value.disk_size_gb

  scaling_config {
    min_size     = each.value.min_nodes
    max_size     = each.value.max_nodes
    desired_size = each.value.min_nodes
  }

  labels = {
    workload-type = each.key
    gpu-type      = each.value.gpu_type
    gpu-count     = tostring(each.value.gpu_count)
  }

  taint {
    key    = "nvidia.com/gpu"
    value  = "present"
    effect = "NO_SCHEDULE"
  }

  tags = merge(var.tags, {
    "k8s.io/cluster-autoscaler/enabled"     = "true"
    "k8s.io/cluster-autoscaler/${var.name}" = "owned"
  })

  lifecycle {
    # The cluster autoscaler owns desired_size after creation.
    ignore_changes = [scaling_config[0].desired_size]
  }

  depends_on = [aws_iam_role_policy_attachment.node]
}

# --- Core addons ---

resource "aws_eks_addon" "core" {
  for_each = toset([
    "vpc-cni",
    "coredns",
    "kube-proxy",
    "eks-pod-identity-agent",
  ])

  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = each.value
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"

  tags = var.tags

  # coredns needs schedulable nodes to leave DEGRADED.
  depends_on = [aws_eks_node_group.system]
}

# --- Worker identity (IRSA) ---
#
# Assumable only by the <workload_namespace>/<workload_ksa_name> Kubernetes
# service account. Annotate that KSA with the role ARN:
#   eks.amazonaws.com/role-arn: <worker_role_arn output>

data "aws_iam_policy_document" "worker_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.this.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.oidc_issuer_host}:sub"
      values   = ["system:serviceaccount:${var.workload_namespace}:${var.workload_ksa_name}"]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.oidc_issuer_host}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "worker" {
  name               = "${var.name}-worker"
  description        = "Used by serving pods (via IRSA) to read model weights from S3"
  assume_role_policy = data.aws_iam_policy_document.worker_assume.json

  tags = var.tags
}

data "aws_iam_policy_document" "worker_models" {
  statement {
    actions   = ["s3:ListBucket"]
    resources = ["arn:aws:s3:::${var.model_bucket_name}"]
  }

  statement {
    actions   = ["s3:GetObject"]
    resources = ["arn:aws:s3:::${var.model_bucket_name}/*"]
  }
}

resource "aws_iam_role_policy" "worker_models" {
  count = var.model_bucket_name != null ? 1 : 0

  name   = "model-bucket-read"
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.worker_models.json
}

# --- Cluster autoscaler identity (IRSA) ---
#
# For the kube-system/cluster-autoscaler service account (the upstream
# cluster-autoscaler Helm chart's default). Mutating actions are scoped to
# ASGs carrying this cluster's discovery tag.

data "aws_iam_policy_document" "cluster_autoscaler_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.this.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.oidc_issuer_host}:sub"
      values   = ["system:serviceaccount:kube-system:cluster-autoscaler"]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.oidc_issuer_host}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "cluster_autoscaler" {
  name               = "${var.name}-cluster-autoscaler"
  description        = "Used by the cluster autoscaler (via IRSA) to scale this cluster's node groups"
  assume_role_policy = data.aws_iam_policy_document.cluster_autoscaler_assume.json

  tags = var.tags
}

data "aws_iam_policy_document" "cluster_autoscaler" {
  statement {
    sid = "Describe"
    actions = [
      "autoscaling:DescribeAutoScalingGroups",
      "autoscaling:DescribeAutoScalingInstances",
      "autoscaling:DescribeLaunchConfigurations",
      "autoscaling:DescribeScalingActivities",
      "autoscaling:DescribeTags",
      "ec2:DescribeImages",
      "ec2:DescribeInstanceTypes",
      "ec2:DescribeLaunchTemplateVersions",
      "ec2:GetInstanceTypesFromInstanceRequirements",
      "eks:DescribeNodegroup",
    ]
    resources = ["*"]
  }

  statement {
    sid = "Scale"
    actions = [
      "autoscaling:SetDesiredCapacity",
      "autoscaling:TerminateInstanceInAutoScalingGroup",
    ]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/k8s.io/cluster-autoscaler/${var.name}"
      values   = ["owned"]
    }
  }
}

resource "aws_iam_role_policy" "cluster_autoscaler" {
  name   = "cluster-autoscaler"
  role   = aws_iam_role.cluster_autoscaler.id
  policy = data.aws_iam_policy_document.cluster_autoscaler.json
}

# --- Optional S3 model bucket ---

resource "aws_s3_bucket" "models" {
  count = var.create_model_bucket ? 1 : 0

  bucket = var.model_bucket_name

  tags = var.tags
}

resource "aws_s3_bucket_public_access_block" "models" {
  count = var.create_model_bucket ? 1 : 0

  bucket = aws_s3_bucket.models[0].id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
