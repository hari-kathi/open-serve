output "network_name" {
  description = "Name of the VPC."
  value       = google_compute_network.vpc.name
}

output "network_id" {
  description = "ID of the VPC."
  value       = google_compute_network.vpc.id
}

output "network_self_link" {
  description = "Self link of the VPC."
  value       = google_compute_network.vpc.self_link
}

output "subnet_name" {
  description = "Name of the subnet."
  value       = google_compute_subnetwork.subnet.name
}

output "subnet_id" {
  description = "ID of the subnet."
  value       = google_compute_subnetwork.subnet.id
}

output "subnet_self_link" {
  description = "Self link of the subnet."
  value       = google_compute_subnetwork.subnet.self_link
}

output "pods_secondary_range_name" {
  description = "Name of the pods secondary range."
  value       = google_compute_subnetwork.subnet.secondary_ip_range[0].range_name
}

output "services_secondary_range_name" {
  description = "Name of the services secondary range."
  value       = google_compute_subnetwork.subnet.secondary_ip_range[1].range_name
}

output "nat_ips" {
  description = "Reserved static NAT IP addresses (empty when auto-allocated)."
  value       = google_compute_address.nat[*].address
}
