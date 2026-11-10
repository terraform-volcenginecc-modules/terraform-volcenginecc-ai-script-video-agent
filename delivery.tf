locals {
  auto_delivery = var.auto_source_upload && var.source_release == null
  source_payload = jsonencode(merge(
    { for name in fileset("${path.module}/backend/app", "**") :
      "backend/app/${name}" => filebase64("${path.module}/backend/app/${name}")
      if endswith(name, ".py") && !can(regex("(^|/)(__pycache__|\\.venv)(/|$)", name))
    },
    { for name in fileset("${path.module}/frontend/comfyui-extension", "**") :
      "frontend/comfyui-extension/${name}" => filebase64("${path.module}/frontend/comfyui-extension/${name}")
    },
    { for name in ["backend/requirements.txt", "backend/comfyui_extension.py", "frontend/login/index.html",
      "frontend/login/login.js", "frontend/nginx/studio.conf", "frontend/nginx/public.conf", "scripts/ecs-native.py",
      "scripts/ecs-public-web.py", "scripts/install-comfyui.py", "scripts/comfyui-requirements.txt", "scripts/comfyui-release.json"] :
      name => filebase64("${path.module}/${name}")
    }
  ))
  source_payload_sha256 = sha256(local.source_payload)
  source_encoded        = base64gzip(local.source_payload)
  source_chunk_size     = 16 * 1024
  source_chunk_count    = ceil(length(local.source_encoded) / local.source_chunk_size)
  source_size_valid     = length(local.source_encoded) <= 10 * 1024 * 1024 && length(local.source_payload) <= 100 * 1024 * 1024
  # Cap range expansion before the resource preconditions reject oversized payloads.
  source_parts = {
    for index in range(min(local.source_chunk_count, 640)) :
    format("%04d", index) => substr(local.source_encoded, index * local.source_chunk_size, local.source_chunk_size)
  }
  source_manifest = jsonencode({
    format         = 1
    payload_format = "base64gzip_json"
    sha256         = local.source_payload_sha256
    encoded_sha256 = sha256(local.source_encoded)
    encoded_size   = length(local.source_encoded)
    chunk_size     = local.source_chunk_size
    chunk_count    = local.source_chunk_count
  })
  delivery_read_resources = var.source_release == null ? [] : [
    "trn:tos:::${var.source_release.bucket}/${var.source_release.object_key}"
  ]
}

# Only the application source parts and manifest are public. The bucket stays private.
resource "volcenginecc_tos_bucket" "source" {
  count         = local.auto_delivery ? 1 : 0
  name          = "${var.name_prefix}-source-${substr(sha256(volcenginecc_vpc_vpc.app.id), 0, 12)}"
  project_name  = var.project_name
  bucket_type   = "fns"
  storage_class = "STANDARD"
  tags          = local.tags
  acl_grant = {
    acl                = "private"
    grant_full_control = ""
    grant_read         = ""
    grant_read_acp     = ""
    grant_write        = ""
    grant_write_acp    = ""
  }
  lifecycle {
    # CCAPI reads ACL but omits ACLGrant; keep creation private and ignore its drift.
    ignore_changes = [acl_grant]
  }
}

resource "volcenginecc_tos_object" "source_parts" {
  for_each               = local.auto_delivery ? local.source_parts : {}
  bucket                 = volcenginecc_tos_bucket.source[0].name
  key                    = "releases/${local.source_payload_sha256}/parts/part-${each.key}.txt"
  content                = each.value
  content_type           = "text/plain; charset=utf-8"
  public_acl             = "public-read"
  server_side_encryption = "AES256"
  storage_class          = "STANDARD"
  tags                   = local.tags
  lifecycle {
    precondition {
      condition     = fileexists("${path.module}/frontend/login/index.html")
      error_message = "缺少 ComfyUI 登录入口文件。"
    }
    precondition {
      condition     = local.source_size_valid
      error_message = "The source payload exceeds the 10 MiB encoded or 100 MiB decoded limit."
    }
  }
}

# Publish the small manifest only after all parts exist; ECS depends on this object.
# Retain the source[0] address, with a new key so legacy payloads are never mistaken
# for manifests. Each CCAPI object request/response remains small.
resource "volcenginecc_tos_object" "source" {
  count                  = local.auto_delivery ? 1 : 0
  bucket                 = volcenginecc_tos_bucket.source[0].name
  key                    = "releases/${local.source_payload_sha256}/manifest.json"
  content                = local.source_manifest
  content_type           = "application/json"
  public_acl             = "public-read"
  server_side_encryption = "AES256"
  storage_class          = "STANDARD"
  tags                   = local.tags
  depends_on             = [volcenginecc_tos_object.source_parts]
  lifecycle {
    precondition {
      condition     = local.source_size_valid
      error_message = "The source payload exceeds the 10 MiB encoded or 100 MiB decoded limit."
    }
  }
}
