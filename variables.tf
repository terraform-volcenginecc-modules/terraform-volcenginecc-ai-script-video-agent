variable "region" {
  description = "Infrastructure region. The Ark endpoint is configured separately."
  type        = string
  default     = "cn-beijing"
}

variable "zone_id" {
  description = "Availability zone with stock for the selected instance type."
  type        = string
  validation {
    condition     = can(regex("^[a-z]{2}-[a-z0-9-]+-[a-z0-9]+$", var.zone_id))
    error_message = "Provide a valid availability zone ID."
  }
}

variable "name_prefix" {
  description = "Lowercase resource name prefix."
  type        = string
  default     = "ai-animation"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,29}$", var.name_prefix))
    error_message = "Use 3-30 lowercase letters, digits or hyphens, starting with a letter."
  }
}

variable "project_name" {
  description = "Existing Volcengine IAM project."
  type        = string
  default     = "default"
}

variable "vpc_cidr" {
  description = "IPv4 VPC CIDR; the first nonzero /24 is used for the application subnet."
  type        = string
  default     = "10.42.0.0/16"
  validation {
    condition = can(cidrnetmask(var.vpc_cidr)) && try(
      tonumber(split("/", var.vpc_cidr)[1]) >= 8 &&
      tonumber(split("/", var.vpc_cidr)[1]) <= 23, false
    )
    error_message = "Provide an IPv4 CIDR with a prefix length between 8 and 23."
  }
}

variable "image_id" {
  description = "Existing Ubuntu 24.04 amd64 ECS image ID with cloud-init enabled."
  type        = string
  validation {
    condition     = can(regex("^image-[a-zA-Z0-9]+$", var.image_id))
    error_message = "Provide a real ECS image ID for the selected region."
  }
}

variable "instance_type" {
  description = "Available CPU ECS type, recommended starting capacity: 4 vCPU / 8 GiB."
  type        = string
  validation {
    condition     = can(regex("^ecs\\.[a-zA-Z0-9.-]+$", var.instance_type))
    error_message = "Provide a valid ECS instance type."
  }
}

variable "ssh_key_pair_name" {
  description = "Existing ECS key pair name for key_pair login; leave null or empty for password login."
  type        = string
  default     = null
  nullable    = true
}

variable "ecs_login_mode" {
  description = "ECS administrator login: key_pair or password."
  type        = string
  default     = "key_pair"
  validation {
    condition     = contains(["key_pair", "password"], var.ecs_login_mode)
    error_message = "Choose key_pair or password."
  }
}

variable "ecs_password" {
  description = "Initial ECS password for password login. Supply a sensitive platform input or TF_VAR_ecs_password; Terraform state/plan contains this value."
  type        = string
  default     = null
  nullable    = true
  sensitive   = true
  validation {
    condition = var.ecs_password == null ? true : (trimspace(var.ecs_password) == "" ? true : (
      length(var.ecs_password) >= 8 && length(var.ecs_password) <= 30 &&
      sum([
        for pattern in ["[A-Z]", "[a-z]", "[0-9]", "[^A-Za-z0-9]"] :
        length(regexall(pattern, var.ecs_password)) > 0 ? 1 : 0
      ]) >= 3 &&
      can(regex("^[A-Za-z0-9`~!@#$%^&*()_+={}\\[\\]:;'<>,.?/|\\-]+$", var.ecs_password)) &&
      !startswith(var.ecs_password, "/") && !startswith(var.ecs_password, "$6$")
    ))
    error_message = "Use 8-30 characters, at least three character classes, and ECS-supported characters; cannot start with / or $6$."
  }
}

variable "ssh_allowed_cidrs" {
  description = "Explicit administrator IPv4 CIDRs allowed to connect to SSH."
  type        = set(string)
  validation {
    condition = length(var.ssh_allowed_cidrs) > 0 && alltrue([
      for cidr in var.ssh_allowed_cidrs :
      can(cidrnetmask(cidr))
    ])
    error_message = "Provide at least one valid administrator IPv4 CIDR."
  }
}

variable "web_allowed_cidrs" {
  description = "IPv4 CIDRs allowed on ports 80/443. Empty keeps web ingress closed."
  type        = set(string)
  default     = []
  validation {
    condition     = alltrue([for cidr in var.web_allowed_cidrs : can(cidrnetmask(cidr))])
    error_message = "Each web source must be an IPv4 CIDR."
  }
}

variable "system_disk_size_gib" {
  description = "Persistent OS, PostgreSQL and work-directory disk size."
  type        = number
  default     = 100
  validation {
    condition     = var.system_disk_size_gib >= 40 && floor(var.system_disk_size_gib) == var.system_disk_size_gib
    error_message = "Use an integer size of at least 40 GiB."
  }
}

variable "system_disk_type" {
  description = "Supported system disk type for the selected instance."
  type        = string
  default     = "ESSD_FlexPL"
  validation {
    condition     = contains(["PTSSD", "ESSD_PL0", "ESSD_FlexPL", "TSSD_TL0"], var.system_disk_type)
    error_message = "Select a disk type supported by the provider and instance."
  }
}

variable "delete_system_disk_with_instance" {
  description = "Delete the system disk with the ECS instance by default. False retains the disk and continues billing."
  type        = bool
  default     = true
}

variable "deletion_protection" {
  description = "Cloud-side ECS deletion protection."
  type        = bool
  default     = false
}

variable "eni_release_wait_seconds" {
  description = "Local wait after ECS deletion and before security group deletion; requires python3 on the Terraform runner. Apply before destroy to persist the barrier."
  type        = number
  default     = 120
  nullable    = false
  validation {
    condition     = var.eni_release_wait_seconds >= 0 && var.eni_release_wait_seconds <= 900 && floor(var.eni_release_wait_seconds) == var.eni_release_wait_seconds
    error_message = "Use an integer number of seconds between 0 and 900; 0 disables the delay."
  }
}

variable "public_bandwidth_mbps" {
  description = "PayByTraffic public IPv4 bandwidth cap in Mbps."
  type        = number
  default     = 10
  validation {
    condition     = var.public_bandwidth_mbps >= 1 && var.public_bandwidth_mbps <= 200 && floor(var.public_bandwidth_mbps) == var.public_bandwidth_mbps
    error_message = "Use an integer bandwidth cap between 1 and 200 Mbps."
  }
}

variable "bucket_name" {
  description = "Globally unique TOS bucket name chosen by the operator."
  type        = string
  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$", var.bucket_name))
    error_message = "Use 3-63 lowercase letters, digits or hyphens with alphanumeric ends."
  }
}

variable "bucket_versioning" {
  description = "TOS bucket versioning state."
  type        = string
  default     = "Enabled"
  validation {
    condition     = contains(["Enabled", "Suspended"], var.bucket_versioning)
    error_message = "Use Enabled or Suspended."
  }
}

variable "cors_allowed_origins" {
  description = "Exact browser origins, such as https://studio.example.com; empty disables CORS."
  type        = set(string)
  default     = []
  validation {
    condition     = alltrue([for origin in var.cors_allowed_origins : can(regex("^https?://[^/\\s]+$", origin))])
    error_message = "Use exact http(s) origins without a path or wildcard."
  }
}

variable "ark_base_url" {
  description = "Ark runtime API base URL."
  type        = string
  default     = "https://ark.cn-beijing.volces.com/api/v3"
  validation {
    condition     = can(regex("^https://[^/\\s]+/api/v3$", var.ark_base_url))
    error_message = "Provide an HTTPS Ark API base URL ending in /api/v3."
  }
}

variable "script_model_id" {
  description = "Default script model; projects may override it in the application."
  type        = string
  default     = "doubao-seed-2-1-pro-260628"
}

variable "video_model_id" {
  description = "Default video model; projects may override it in the application."
  type        = string
  default     = "doubao-seedance-2-0-260128"
}

variable "application_compose_path" {
  description = "Removed cloud Compose mode. Leave null; cloud deployment uses native services."
  type        = string
  default     = null
  nullable    = true
  validation {
    condition     = var.application_compose_path == null
    error_message = "Cloud Compose mode was removed. Leave application_compose_path null and use native source delivery."
  }
}

variable "source_release" {
  description = "Optional immutable source object in an EXISTING private TOS delivery bucket. No signed URLs or secrets."
  type = object({
    bucket                = string
    region                = string
    endpoint              = string
    object_key            = string
    sha256                = string
    version_id            = optional(string)
    ark_secret_object_key = optional(string)
  })
  default  = null
  nullable = true
  validation {
    condition = var.source_release == null ? true : (
      can(regex("^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$", var.source_release.bucket)) &&
      can(regex("^[a-z]{2}-[a-z0-9-]+$", var.source_release.region)) &&
      can(regex("^https://tos(-s3)?-[a-z0-9-]+\\.volces\\.com$", var.source_release.endpoint)) &&
      can(regex("^[a-f0-9]{64}$", var.source_release.sha256)) &&
      startswith(var.source_release.object_key, "releases/") &&
      !can(regex("[*?\\n\\r]", var.source_release.object_key)) &&
      (var.source_release.ark_secret_object_key == null ? true :
        length(var.source_release.ark_secret_object_key) > 0 &&
      !can(regex("[*?\\n\\r]", var.source_release.ark_secret_object_key)))
    )
    error_message = "Use a private TOS bucket, regional HTTPS endpoint, releases/ object key without wildcards, and lowercase SHA256."
  }
}

variable "auto_source_upload" {
  description = "Automatically upload native source and prebuilt frontend as 16 KiB public-read TOS parts plus a checksum manifest during apply. Existing source_release takes precedence."
  type        = bool
  default     = true
}

variable "ark_secret_source" {
  description = "Optional EXISTING private TOS object containing a single-line Ark key. The template stores only this reference."
  type = object({
    bucket     = string
    region     = string
    endpoint   = string
    object_key = string
    version_id = optional(string)
  })
  default  = null
  nullable = true
  validation {
    condition = var.ark_secret_source == null ? true : (
      can(regex("^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$", var.ark_secret_source.bucket)) &&
      can(regex("^[a-z]{2}-[a-z0-9-]+$", var.ark_secret_source.region)) &&
      can(regex("^https://tos(-s3)?-[a-z0-9-]+\\.volces\\.com$", var.ark_secret_source.endpoint)) &&
      length(var.ark_secret_source.object_key) > 0 &&
      !can(regex("[*?\\n\\r]", var.ark_secret_source.object_key))
    )
    error_message = "Use an existing private bucket, regional HTTPS endpoint and object key without wildcards."
  }
}

variable "tags" {
  description = "Additional resource tags."
  type        = map(string)
  default     = {}
}
