output "infrastructure" {
  description = "Created network and compute identifiers."
  value = {
    vpc_id            = volcenginecc_vpc_vpc.app.id
    subnet_id         = volcenginecc_vpc_subnet.app.id
    subnet_cidr       = local.subnet_cidr
    security_group_id = volcenginecc_vpc_security_group.app.id
    instance_id       = volcenginecc_ecs_instance.app.id
    public_ip         = volcenginecc_ecs_instance.app.eip_address.ip_address
  }
}

output "asset_storage" {
  description = "Private bucket details; no access credentials."
  value = {
    bucket            = volcenginecc_tos_bucket.assets.name
    intranet_endpoint = volcenginecc_tos_bucket.assets.intranet_endpoint
    extranet_endpoint = volcenginecc_tos_bucket.assets.extranet_endpoint
    role_trn          = volcenginecc_iam_role.app.trn
  }
}

output "runtime_config" {
  description = "Non-secret application configuration, also written to the ECS host."
  value       = local.runtime_config
}

output "application_access" {
  description = "Public HTTP/HTTPS when web ingress is allowed, plus an SSH tunnel. HTTPS initially uses a self-signed certificate."
  value = {
    local_url              = "http://127.0.0.1:8080"
    public_http_url        = length(var.web_allowed_cidrs) > 0 ? "http://${volcenginecc_ecs_instance.app.eip_address.ip_address}" : null
    public_https_url       = length(var.web_allowed_cidrs) > 0 ? "https://${volcenginecc_ecs_instance.app.eip_address.ip_address}" : null
    tls_certificate_path   = "/etc/nginx/ssl/ai-animation/tls.crt"
    tls_private_key_path   = "/etc/nginx/ssl/ai-animation/tls.key"
    ssh_tunnel_command     = "ssh -N -L 8080:127.0.0.1:8080 <login-user>@${volcenginecc_ecs_instance.app.eip_address.ip_address}"
    config_path            = "/opt/ai-animation/config/runtime.json"
    deployment_mode        = "ecs_native"
    database_data_path     = "/var/lib/postgresql/16/main"
    ark_secret_path        = "/opt/ai-animation/secrets/ark_api_key"
    start_command          = "sudo systemctl start ai-animation.service"
    web_ingress_enabled    = length(var.web_allowed_cidrs) > 0
    source_deploy_enabled  = local.deployment_enabled
    ecs_login_mode         = var.ecs_login_mode
    deployment_status_path = "/opt/ai-animation/deployment-status.json"
    status_command         = "sudo systemctl status ai-animation.service ai-animation-comfyui.service ai-animation-api.service ai-animation-worker.service nginx.service; sudo cat /opt/ai-animation/deployment-status.json"
    log_command            = "sudo journalctl -u ai-animation.service -u ai-animation-comfyui.service -u ai-animation-api.service -u ai-animation-worker.service -n 100 --no-pager"
  }
}

output "source_delivery" {
  description = "Non-secret source location. Automatic source objects are public-read; the source and asset buckets have private bucket ACLs."
  value = {
    mode           = local.auto_delivery ? "auto_public" : (var.source_release == null ? "infrastructure_only" : "external_private")
    bucket         = local.auto_delivery ? volcenginecc_tos_bucket.source[0].name : (var.source_release == null ? null : var.source_release.bucket)
    object_key     = local.auto_delivery ? volcenginecc_tos_object.source[0].key : (var.source_release == null ? null : var.source_release.object_key)
    sha256         = local.auto_delivery ? local.source_payload_sha256 : (var.source_release == null ? null : var.source_release.sha256)
    public_url     = local.auto_delivery ? split("?", volcenginecc_tos_object.source[0].url)[0] : null
    payload_format = local.auto_delivery ? "base64gzip_json_chunks" : (var.source_release == null ? null : "tar_gzip")
    chunk_count    = local.auto_delivery ? local.source_chunk_count : null
  }
}
