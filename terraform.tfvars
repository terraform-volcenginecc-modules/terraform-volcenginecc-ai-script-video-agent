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

# Default: key pair login. Supply an existing name, no private key is uploaded.
ecs_login_mode = "key_pair"
# Key-pair login must explicitly disable the example password default.
ecs_password      = null
ssh_key_pair_name = "wordpress"

# Alternative: password login. Set the key-pair name to null or empty.
# ecs_login_mode    = "password"
# ssh_key_pair_name = null
# Supply ecs_password through TF_VAR_ecs_password or a sensitive platform field.
# Password is stored in Terraform state/plan; do not commit it to this file.

# Default: Terraform creates a private source bucket, uploads a public-read
# content-addressed native runtime + prebuilt frontend, and starts ECS services.
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

# Cloud deployment is native; leave application_compose_path null.

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
