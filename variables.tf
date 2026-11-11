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
  description = "密码登录时必须提供 ECS 初始密码；密钥登录时留空。通过平台敏感字段或 TF_VAR_ecs_password 注入，值会进入 Terraform State 和 Plan。"
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
  description = "Destroy-only time_sleep delay after ECS deletion and before security group deletion. Apply before destroy to persist the barrier."
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

variable "ark_api_key" {
  description = "Required Ark API key written to the ECS backend secret file. Supply it through a sensitive platform input or TF_VAR_ark_api_key; Terraform state contains this value."
  type        = string
  nullable    = false
  sensitive   = true
  validation {
    condition = (
      trimspace(var.ark_api_key) != "" &&
      length(var.ark_api_key) <= 16384 &&
      !can(regex("[\\n\\r\\x00]", var.ark_api_key))
    )
    error_message = "ark_api_key is required and must be a nonempty single-line value."
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

variable "video_provider" {
  description = "视频服务选择；ark 为默认值，comfyui 调用独立 GPU 服务，剧本始终使用 Ark。"
  type        = string
  default     = "ark"
  nullable    = false
  validation {
    condition     = contains(["ark", "comfyui"], var.video_provider)
    error_message = "video_provider 只能选择 ark 或 comfyui。"
  }
}

variable "comfyui_api_key" {
  description = "ComfyUI Bearer 认证密钥；选择 comfyui 时必填，直接写入 ECS 密钥文件，敏感值仍进入 Terraform State。"
  type        = string
  default     = ""
  nullable    = false
  sensitive   = true
  validation {
    condition     = length(var.comfyui_api_key) <= 16384 && !can(regex("[\\n\\r\\x00]", var.comfyui_api_key))
    error_message = "ComfyUI 密钥必须是长度不超过 16384 的单行值。"
  }
}

variable "comfyui" {
  description = "远程 Comfy API v2 契约；只接受真实 API 格式 Workflow JSON 和明确节点映射，不安装 GPU 服务或模型。"
  type = object({
    base_url       = string
    workflow_json  = string
    output_node_id = string
    output_index   = optional(number, 0)
    bindings = object({
      prompt           = object({ node_id = string, input = string })
      duration         = object({ node_id = string, input = string })
      width            = object({ node_id = string, input = string })
      height           = object({ node_id = string, input = string })
      seed             = object({ node_id = string, input = string })
      generate_audio   = optional(object({ node_id = string, input = string }))
      watermark        = optional(object({ node_id = string, input = string }))
      first_frame      = optional(object({ node_id = string, input = string }))
      last_frame       = optional(object({ node_id = string, input = string }))
      reference_images = optional(list(object({ node_id = string, input = string })))
    })
    capabilities = object({
      modes          = list(string)
      resolutions    = list(string)
      ratios         = list(string)
      min_duration   = number
      max_duration   = number
      generate_audio = list(bool)
      watermark      = list(bool)
    })
  })
  default = null
  validation {
    condition = var.comfyui == null ? true : (
      can(regex("^https?://[^/?#\\s@]+(/[^?#\\s]*)?$", var.comfyui.base_url)) &&
      can(jsondecode(var.comfyui.workflow_json)) &&
      try(length(keys(jsondecode(var.comfyui.workflow_json))) > 0 &&
        !contains(keys(jsondecode(var.comfyui.workflow_json)), "nodes") &&
      !contains(keys(jsondecode(var.comfyui.workflow_json)), "links"), false) &&
      var.comfyui.output_index >= 0 && floor(var.comfyui.output_index) == var.comfyui.output_index
    )
    error_message = "ComfyUI 地址须为 HTTP(S) 服务地址；必须提供非空 API 格式 Workflow JSON，输出序号须为非负整数。"
  }
}

variable "media_url_ttl_seconds" {
  description = "素材签名地址有效期（秒）；实例角色凭证剩余有效期不足时会自动缩短。"
  type        = number
  default     = 900
  validation {
    condition     = var.media_url_ttl_seconds >= 60 && var.media_url_ttl_seconds <= 3600 && floor(var.media_url_ttl_seconds) == var.media_url_ttl_seconds
    error_message = "素材签名地址有效期须为 60 到 3600 之间的整数。"
  }
}

variable "tags" {
  description = "Additional resource tags."
  type        = map(string)
  default     = {}
}
