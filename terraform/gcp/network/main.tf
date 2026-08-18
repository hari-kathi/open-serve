# VPC + GKE-ready regional subnet (pods/services secondary ranges) + Cloud NAT.
# Private nodes reach the internet (image registries, model hubs) through NAT.

resource "google_compute_network" "vpc" {
  name                    = var.name
  project                 = var.project_id
  auto_create_subnetworks = false
  routing_mode            = "REGIONAL"
}

resource "google_compute_subnetwork" "subnet" {
  name                     = "${var.name}-subnet"
  project                  = var.project_id
  region                   = var.region
  network                  = google_compute_network.vpc.id
  ip_cidr_range            = var.subnet_cidr_range
  private_ip_google_access = true

  secondary_ip_range {
    range_name    = "${var.name}-pods"
    ip_cidr_range = var.pods_cidr_range
  }

  secondary_ip_range {
    range_name    = "${var.name}-services"
    ip_cidr_range = var.services_cidr_range
  }

  dynamic "log_config" {
    for_each = var.enable_flow_logs ? [1] : []
    content {
      aggregation_interval = "INTERVAL_5_SEC"
      flow_sampling        = 0.5
      metadata             = "INCLUDE_ALL_METADATA"
    }
  }
}

resource "google_compute_router" "router" {
  name    = "${var.name}-router"
  project = var.project_id
  region  = var.region
  network = google_compute_network.vpc.id
}

# Static NAT IPs (stable egress addresses, useful for allowlisting).
# With nat_ip_count = 0, Cloud NAT auto-allocates ephemeral IPs instead.
resource "google_compute_address" "nat" {
  count = var.nat_ip_count

  name    = "${var.name}-nat-ip-${count.index}"
  project = var.project_id
  region  = var.region
}

resource "google_compute_router_nat" "nat" {
  name    = "${var.name}-nat"
  project = var.project_id
  router  = google_compute_router.router.name
  region  = google_compute_router.router.region

  nat_ip_allocate_option = var.nat_ip_count > 0 ? "MANUAL_ONLY" : "AUTO_ONLY"
  nat_ips                = google_compute_address.nat[*].self_link

  source_subnetwork_ip_ranges_to_nat = "LIST_OF_SUBNETWORKS"

  subnetwork {
    name                    = google_compute_subnetwork.subnet.id
    source_ip_ranges_to_nat = ["ALL_IP_RANGES"]
  }
}
