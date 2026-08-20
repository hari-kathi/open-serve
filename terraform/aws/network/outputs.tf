output "vpc_id" {
  description = "ID of the VPC."
  value       = aws_vpc.this.id
}

output "vpc_cidr" {
  description = "CIDR block of the VPC."
  value       = aws_vpc.this.cidr_block
}

output "private_subnet_ids" {
  description = "IDs of the private subnets (EKS nodes)."
  value       = aws_subnet.private[*].id
}

output "public_subnet_ids" {
  description = "IDs of the public subnets (NAT gateways, external load balancers)."
  value       = aws_subnet.public[*].id
}

output "availability_zones" {
  description = "Availability zones the subnets are spread across."
  value       = local.azs
}

output "nat_public_ips" {
  description = "Public IPs of the NAT gateways (stable egress addresses, useful for allowlisting)."
  value       = aws_eip.nat[*].public_ip
}
