mock_provider "volcenginecc" {
  mock_resource "volcenginecc_tos_object" {
    defaults = { url = "https://verification-source.tos-cn-beijing.volces.com/releases/test/manifest.json?signature=mock" }
  }
  mock_resource "volcenginecc_iam_role" {
    defaults = { trn = "trn:iam::100000000000:role/ai-animation-ecs" }
  }
  mock_resource "volcenginecc_tos_bucket" {
    defaults = {
      intranet_endpoint = "tos-cn-beijing.ivolces.com"
      extranet_endpoint = "tos-cn-beijing.volces.com"
    }
  }
}

variables {
  zone_id            = "cn-beijing-a"
  image_id           = "image-verification"
  instance_type      = "ecs.g3i.xlarge"
  ssh_key_pair_name  = "verification-key"
  ssh_allowed_cidrs  = ["192.0.2.1/32"]
  bucket_name        = "verification-assets-only"
  auto_source_upload = false
  # Exercise cleanup without waiting for real cloud ENI release in offline tests.
  eni_release_wait_seconds = 0
}

run "legacy_without_source" {
  command = apply
  assert {
    condition     = terraform_data.eni_release_wait.input.wait_seconds == 0
    error_message = "The release barrier must persist the configured delay."
  }
  assert {
    condition     = !output.application_access.source_deploy_enabled && length(volcenginecc_iam_policy.delivery) == 0
    error_message = "Legacy provisioning must remain opt-in."
  }
  assert {
    condition     = length(volcenginecc_tos_object.source_parts) == 0 && length(volcenginecc_tos_object.source) == 0
    error_message = "Infrastructure-only provisioning must not create source parts or a manifest."
  }
}

run "source_with_secret" {
  command = apply
  variables {
    source_release = {
      bucket                = "verification-delivery-only"
      region                = "cn-beijing"
      endpoint              = "https://tos-cn-beijing.volces.com"
      object_key            = "releases/v1/source.tar.gz"
      sha256                = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      version_id            = "example-version"
      ark_secret_object_key = "deployment-secrets/ark_api_key"
    }
  }
  assert {
    condition     = length(base64encode(local.cloud_init)) <= 21332
    error_message = "Rendered user-data must remain within ECS Linux limit."
  }
  assert {
    condition     = length(jsondecode(volcenginecc_iam_policy.delivery[0].policy_document).Statement[0].Resource) == 2
    error_message = "The delivery policy must grant only source and secret objects."
  }
  assert {
    condition     = contains([for command in jsondecode(substr(local.cloud_init, 14, length(local.cloud_init) - 14)).runcmd : join(" ", command)], "systemctl enable --now ai-animation.service")
    error_message = "Source deployment must start automatically."
  }
}

run "source_without_secret" {
  command = apply
  variables {
    source_release = {
      bucket     = "verification-delivery-only"
      region     = "cn-beijing"
      endpoint   = "https://tos-cn-beijing.volces.com"
      object_key = "releases/v1/source.tar.gz"
      sha256     = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    }
  }
  assert {
    condition     = length(jsondecode(volcenginecc_iam_policy.delivery[0].policy_document).Statement[0].Resource) == 1
    error_message = "An omitted secret must not expand object access."
  }
}

run "wildcard_rejected" {
  command = plan
  variables {
    source_release = {
      bucket     = "verification-delivery-only"
      region     = "cn-beijing"
      endpoint   = "https://tos-cn-beijing.volces.com"
      object_key = "releases/*"
      sha256     = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    }
  }
  expect_failures = [var.source_release]
}

run "region_zone_mismatch_rejected" {
  command = plan
  variables {
    zone_id = "cn-shanghai-a"
  }
  expect_failures = [volcenginecc_ecs_instance.app]
}

run "mutually_exclusive_delivery_rejected" {
  command = plan
  variables {
    application_compose_path = "./compose.yaml"
    source_release = {
      bucket     = "verification-delivery-only"
      region     = "cn-beijing"
      endpoint   = "https://tos-cn-beijing.volces.com"
      object_key = "releases/v1/source.tar.gz"
      sha256     = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    }
  }
  expect_failures = [var.application_compose_path]
}

run "auto_public_key_pair" {
  command = apply
  variables {
    auto_source_upload = true
  }
  assert {
    condition     = output.source_delivery.mode == "auto_public" && volcenginecc_tos_object.source[0].public_acl == "public-read" && volcenginecc_tos_bucket.source[0].acl_grant.acl == "private" && volcenginecc_tos_bucket.assets.acl_grant.acl == "private"
    error_message = "Only the source object may be public."
  }
  assert {
    condition     = local.runtime_config.storage.auto_upload && local.runtime_config.storage.bucket == volcenginecc_tos_bucket.assets.name && local.runtime_config.infrastructure.role_name == volcenginecc_iam_role.app.role_name
    error_message = "Asset backups must use the private asset bucket and the ECS instance role."
  }
  assert {
    condition     = !strcontains(output.source_delivery.public_url, "?") && local.deployment_config.payload_format == "base64gzip_json_chunks"
    error_message = "Public download must not require a signature."
  }
  assert {
    condition     = length(volcenginecc_tos_object.source_parts) == local.source_chunk_count && alltrue([for part in volcenginecc_tos_object.source_parts : length(part.content) <= 16384 && part.public_acl == "public-read"]) && join("", [for key in sort(keys(volcenginecc_tos_object.source_parts)) : volcenginecc_tos_object.source_parts[key].content]) == local.source_encoded
    error_message = "Bounded source objects must reconstruct the exact encoded payload in numbered order."
  }
  assert {
    condition     = jsondecode(volcenginecc_tos_object.source[0].content).encoded_sha256 == sha256(local.source_encoded) && local.deployment_config.manifest_sha256 == sha256(volcenginecc_tos_object.source[0].content) && length(volcenginecc_tos_object.source[0].content) < 16384 && endswith(volcenginecc_tos_object.source[0].key, "/manifest.json") && output.source_delivery.chunk_count == local.source_chunk_count
    error_message = "The small manifest and bootstrap must pin the encoded source and manifest checksums."
  }
  assert {
    condition     = length(base64encode(local.cloud_init)) <= 21332 && !jsondecode(substr(local.cloud_init, 14, length(local.cloud_init) - 14)).ssh_pwauth
    error_message = "Key login cloud-init must fit and disable password authentication."
  }
  assert {
    condition     = contains(keys(jsondecode(local.source_payload)), "frontend/dist/index.html") && contains(keys(jsondecode(local.source_payload)), "scripts/ecs-native.py") && !contains(keys(jsondecode(local.source_payload)), "config/runtime.json") && !contains(keys(jsondecode(local.source_payload)), "frontend/package-lock.json")
    error_message = "Native runtime and prebuilt frontend must be included; credentials and npm dependencies must be excluded."
  }
  assert {
    condition     = !contains(jsondecode(substr(local.cloud_init, 14, length(local.cloud_init) - 14)).packages, "docker.io") && contains(jsondecode(substr(local.cloud_init, 14, length(local.cloud_init) - 14)).packages, "postgresql-16") && output.application_access.deployment_mode == "ecs_native"
    error_message = "Cloud startup must install native PostgreSQL without Docker."
  }
  assert {
    condition     = !jsondecode(substr(local.cloud_init, 14, length(local.cloud_init) - 14)).ssh.emit_keys_to_console && join(" ", jsondecode(substr(local.cloud_init, 14, length(local.cloud_init) - 14)).bootcmd[0]) == "install -d -m 0755 /run/sshd" && strcontains(join(" ", jsondecode(substr(local.cloud_init, 14, length(local.cloud_init) - 14)).bootcmd[1]), "/etc/systemd/system/sshd.service")
    error_message = "SSH compatibility must be prepared before set-passwords; console fingerprint output must be disabled."
  }
  assert {
    condition     = contains([for command in jsondecode(substr(local.cloud_init, 14, length(local.cloud_init) - 14)).runcmd : join(" ", command)], "systemctl reload-or-restart ssh.service")
    error_message = "Final SSH setup must also start an inactive service."
  }
  assert {
    condition     = strcontains(jsondecode(substr(local.cloud_init, 14, length(local.cloud_init) - 14)).bootcmd[2][2], "cloud_final_modules") && strcontains(jsondecode(substr(local.cloud_init, 14, length(local.cloud_init) - 14)).bootcmd[2][2], "keys-to-console")
    error_message = "Cloud-init 20.3 must skip the console module while retaining other final modules."
  }
}

run "auto_public_password_with_private_secret" {
  command = apply
  variables {
    auto_source_upload = true
    ecs_login_mode     = "password"
    ssh_key_pair_name  = ""
    ecs_password       = "Offline-Test-Password123!"
    ark_secret_source = {
      bucket     = "verification-secret-only"
      region     = "cn-beijing"
      endpoint   = "https://tos-cn-beijing.volces.com"
      object_key = "deployment-secrets/ark_api_key"
    }
  }
  assert {
    condition     = output.application_access.ecs_login_mode == "password" && jsondecode(substr(local.cloud_init, 14, length(local.cloud_init) - 14)).ssh_pwauth && nonsensitive(volcenginecc_ecs_instance.app.password == var.ecs_password)
    error_message = "Password mode must submit the ECS password and enable SSH password authentication."
  }
  assert {
    condition     = jsondecode(volcenginecc_iam_policy.delivery[0].policy_document).Statement[0].Resource[0] == "trn:tos:::verification-secret-only/deployment-secrets/ark_api_key" && length(jsondecode(volcenginecc_iam_policy.delivery[0].policy_document).Statement[0].Resource) == 1
    error_message = "Public source mode should authorize only the private secret."
  }
  assert {
    condition     = !strcontains(local.cloud_init, "Offline-Test-Password123!")
    error_message = "ECS password must not be embedded in user-data."
  }
}

run "missing_key_pair_rejected" {
  command = plan
  variables { ssh_key_pair_name = null }
  expect_failures = [volcenginecc_ecs_instance.app]
}

run "missing_password_rejected" {
  command = plan
  variables {
    ecs_login_mode    = "password"
    ssh_key_pair_name = null
    ecs_password      = null
  }
  expect_failures = [volcenginecc_ecs_instance.app]
}

run "conflicting_credentials_rejected" {
  command = plan
  variables { ecs_password = "Offline-Test-Password123!" }
  expect_failures = [volcenginecc_ecs_instance.app]
}

run "weak_password_rejected" {
  command = plan
  variables {
    ecs_login_mode    = "password"
    ssh_key_pair_name = null
    ecs_password      = "simple"
  }
  expect_failures = [var.ecs_password]
}

run "invalid_eni_wait_rejected" {
  command = plan
  variables {
    eni_release_wait_seconds = -1
  }
  expect_failures = [var.eni_release_wait_seconds]
}
