locals {
  ecs_key_pair = var.ssh_key_pair_name == null ? null : (trimspace(var.ssh_key_pair_name) == "" ? null : var.ssh_key_pair_name)
  ecs_password = var.ecs_password == null ? null : (trimspace(var.ecs_password) == "" ? null : var.ecs_password)
  # Terraform 1.5 rejects nonsensitive() when a null-derived boolean is already public.
  ecs_has_password = try(nonsensitive(local.ecs_password != null), local.ecs_password != null)
  subnet_cidr      = cidrsubnet(var.vpc_cidr, 24 - tonumber(split("/", var.vpc_cidr)[1]), 1)
  tags = [
    for key, value in merge({
      managed_by = "terraform"
      solution   = "ai-animation"
    }, var.tags) : { key = key, value = value }
  ]
  ingress_rules = concat(
    [for cidr in sort(tolist(var.ssh_allowed_cidrs)) : { cidr = cidr, port = 22 }],
    flatten([
      for cidr in sort(tolist(var.web_allowed_cidrs)) : [
        for port in [80, 443] : { cidr = cidr, port = port }
      ]
    ])
  )
}

resource "volcenginecc_vpc_vpc" "app" {
  vpc_name    = "${var.name_prefix}-vpc"
  description = "AI animation application network"
  cidr_block  = var.vpc_cidr
  # Direct ECS public-IP access uses a classic VPC; no IPv4 gateway is provisioned.
  support_ipv_4_gateway = false
  enable_ipv_6          = false
  project_name          = var.project_name
  tags                  = local.tags
}

resource "volcenginecc_vpc_subnet" "app" {
  subnet_name = "${var.name_prefix}-subnet"
  description = "AI animation application subnet"
  vpc_id      = volcenginecc_vpc_vpc.app.id
  cidr_block  = local.subnet_cidr
  zone_id     = var.zone_id
  tags        = local.tags
}

resource "volcenginecc_vpc_security_group" "app" {
  # Serialize VPC child creation to reduce conflicts during initial provisioning.
  depends_on = [volcenginecc_vpc_subnet.app]

  vpc_id              = volcenginecc_vpc_vpc.app.id
  security_group_name = "${var.name_prefix}-sg"
  description         = "AI animation application ingress"
  project_name        = var.project_name
  tags                = local.tags

  # Every configurable rule field is specified to avoid partial nested Set values.
  ingress_permissions = [
    for rule in local.ingress_rules : {
      cidr_ip         = rule.cidr
      description     = "Allow application port ${rule.port}"
      direction       = "ingress"
      policy          = "accept"
      port_start      = rule.port
      port_end        = rule.port
      priority        = 1
      protocol        = "tcp"
      prefix_list_id  = ""
      source_group_id = ""
    }
  ]
  egress_permissions = [{
    cidr_ip         = "0.0.0.0/0"
    description     = "Allow DNS package downloads and model APIs"
    direction       = "egress"
    policy          = "accept"
    port_start      = -1
    port_end        = -1
    priority        = 1
    protocol        = "all"
    prefix_list_id  = ""
    source_group_id = ""
  }]
}

resource "volcenginecc_tos_bucket" "assets" {
  name                  = var.bucket_name
  project_name          = var.project_name
  bucket_type           = "fns"
  storage_class         = "STANDARD"
  enable_version_status = var.bucket_versioning
  tags                  = local.tags
  acl_grant = {
    acl                = "private"
    grant_full_control = ""
    grant_read         = ""
    grant_read_acp     = ""
    grant_write        = ""
    grant_write_acp    = ""
  }

  # Object deletion is a separate, intentional data-cleanup operation.
  lifecycle {
    prevent_destroy = false
    # CCAPI reads ACL but omits ACLGrant; keep creation private and ignore its drift.
    ignore_changes = [acl_grant]
  }
}

resource "volcenginecc_tos_bucket_cors" "assets" {
  count       = length(var.cors_allowed_origins) > 0 ? 1 : 0
  bucket_name = volcenginecc_tos_bucket.assets.name
  cors_rules = [{
    allowed_origins = sort(tolist(var.cors_allowed_origins))
    allowed_methods = ["GET", "HEAD", "PUT", "POST"]
    allowed_headers = ["*"]
    expose_headers  = ["ETag", "x-tos-request-id"]
    max_age_seconds = 3600
    response_vary   = true
  }]
}

resource "volcenginecc_iam_policy" "assets" {
  policy_name = "${var.name_prefix}-assets"
  policy_type = "Custom"
  description = "AI animation access to its private asset bucket"
  policy_document = jsonencode({
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["tos:ListBucket", "tos:HeadBucket"]
        Resource = ["trn:tos:::${volcenginecc_tos_bucket.assets.name}"]
      },
      {
        Effect   = "Allow"
        Action   = ["tos:GetObject", "tos:PutObject", "tos:DeleteObject"]
        Resource = ["trn:tos:::${volcenginecc_tos_bucket.assets.name}/*"]
      }
    ]
  })
}

resource "volcenginecc_iam_role" "app" {
  role_name            = "${var.name_prefix}-ecs"
  description          = "AI animation ECS runtime role"
  max_session_duration = 3600
  tags                 = local.tags
  trust_policy_document = jsonencode({
    Statement = [{
      Effect    = "Allow"
      Action    = ["sts:AssumeRole"]
      Principal = { Service = ["ecs"] }
    }]
  })
  # This is the only owner of the policy attachment.
  policies = concat([{
    policy_name = volcenginecc_iam_policy.assets.policy_name
    policy_type = volcenginecc_iam_policy.assets.policy_type
    }], [for policy in volcenginecc_iam_policy.delivery : {
    policy_name = policy.policy_name
    policy_type = policy.policy_type
  }])
}

resource "volcenginecc_iam_policy" "delivery" {
  count       = length(local.delivery_read_resources) > 0 ? 1 : 0
  policy_name = "${var.name_prefix}-delivery"
  policy_type = "Custom"
  description = "Read only the pinned source release"
  policy_document = jsonencode({
    Statement = [{
      Effect   = "Allow"
      Action   = ["tos:GetObject"]
      Resource = distinct(local.delivery_read_resources)
    }]
  })
}

# Creation: security group -> release barrier -> ECS.
# Destruction reverses this order, so the delay runs AFTER ECS deletion.
# The time provider handles the delay without invoking local commands.
resource "time_sleep" "eni_release_wait" {
  depends_on = [volcenginecc_vpc_security_group.app]

  destroy_duration = "${var.eni_release_wait_seconds}s"
  triggers = {
    security_group_id = volcenginecc_vpc_security_group.app.id
  }
}

resource "volcenginecc_ecs_instance" "app" {
  depends_on = [time_sleep.eni_release_wait]

  instance_name        = "${var.name_prefix}-app"
  hostname             = "${var.name_prefix}-app"
  description          = "AI animation application host"
  zone_id              = var.zone_id
  instance_type        = var.instance_type
  instance_charge_type = "PostPaid"
  spot_strategy        = "NoSpot"
  project_name         = var.project_name
  deletion_protection  = var.deletion_protection
  tags                 = local.tags

  image = {
    image_id = var.image_id
  }
  key_pair = var.ecs_login_mode == "key_pair" ? { key_pair_name = local.ecs_key_pair } : null
  password = var.ecs_login_mode == "password" ? local.ecs_password : null
  primary_network_interface = {
    subnet_id          = volcenginecc_vpc_subnet.app.id
    security_group_ids = [volcenginecc_vpc_security_group.app.id]
  }
  system_volume = {
    size                 = var.system_disk_size_gib
    volume_type          = var.system_disk_type
    delete_with_instance = var.delete_system_disk_with_instance
  }
  eip_address = {
    bandwidth_mbps        = var.public_bandwidth_mbps
    charge_type           = "PayByTraffic"
    isp                   = "BGP"
    release_with_instance = true
  }
  role_names = [volcenginecc_iam_role.app.role_name]
  user_data  = base64encode(local.cloud_init)

  lifecycle {
    precondition {
      condition     = var.video_provider != "comfyui" || (var.comfyui != null && trimspace(var.comfyui_api_key) != "")
      error_message = "选择 comfyui 时必须同时提供远程服务工作流配置和 comfyui_api_key；ark_api_key 仍为必填。"
    }
    precondition {
      condition = (
        (var.ecs_login_mode == "key_pair" && local.ecs_key_pair != null && !local.ecs_has_password) ||
        (var.ecs_login_mode == "password" && local.ecs_key_pair == null && local.ecs_has_password)
      )
      error_message = "key_pair requires only ssh_key_pair_name; password requires only ecs_password. Leave the other credential null or empty."
    }
    precondition {
      condition     = startswith(var.zone_id, "${var.region}-")
      error_message = "zone_id must belong to region."
    }
    precondition {
      # Base64 length accounts for UTF-8 bytes in the bootstrap configuration.
      condition     = length(base64encode(local.cloud_init)) <= 21332
      error_message = "Cloud-init exceeds the Linux user-data size limit; shorten the supplied configuration or bootstrap scripts."
    }
  }
}
