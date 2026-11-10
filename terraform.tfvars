# Copy to terraform.tfvars and set the REQUIRED values from your cloud account.
# Supply secrets through sensitive platform inputs or TF_VAR_* environment variables.
region       = "cn-beijing"
name_prefix  = "ai-animation"
project_name = "default"
vpc_cidr     = "10.42.0.0/16"

# REQUIRED: uncomment and provide real values.
zone_id           = "cn-beijing-a"
image_id          = "image-ydf193g9cqb6uoeqqqj8"
instance_type     = "ecs.c3a.xlarge"
bucket_name       = "ai-script-video-assets"
ssh_allowed_cidrs = ["0.0.0.0/0"]

# 使用密码登录，不再依赖未下载的 SSH 私钥。
ecs_login_mode    = "password"
ssh_key_pair_name = null
# 通过本地部署脚本的 --ecs-password-file 或平台敏感字段传入 ecs_password。
# 密码会进入 Terraform State 和 Plan，不将正文写入此文件。

# Default: Terraform creates a private source bucket, uploads a public-read
# application source and ComfyUI extensions, and starts ECS services.
auto_source_upload = true

# REQUIRED: set ark_api_key as a sensitive platform input or TF_VAR_ark_api_key.
# Do not commit the actual key to this file. Direct input stores it in Terraform state.

# Add allowed browser access networks when the app is ready.
web_allowed_cidrs = ["0.0.0.0/0"]

# Add exact origins to enable private TOS browser upload/download.
cors_allowed_origins = []

system_disk_size_gib             = 100
system_disk_type                 = "ESSD_FlexPL"
delete_system_disk_with_instance = true
public_bandwidth_mbps            = 10
bucket_versioning                = "Enabled"

ark_base_url    = "https://ark.cn-beijing.volces.com/api/v3"
script_model_id = "doubao-seed-2-1-pro-260628"
video_model_id  = "doubao-seedance-2-0-260128"


# Alternative: private, pre-uploaded archive. Overrides auto_source_upload.
# Existing private delivery bucket; separate from the asset bucket above.
# Generate object_key + sha256 with scripts/package-release.py.
# source_release = {
#   bucket     = "your-existing-delivery-bucket"
#   region     = "cn-beijing"
#   endpoint   = "https://tos-cn-beijing.volces.com"
#   object_key = "releases/v1/<full-sha256>/ai-animation-v1-<short-sha256>.tar.gz"
#   sha256     = "<64 lowercase hexadecimal characters>"
#   version_id = "<optional TOS version ID>"
# }
