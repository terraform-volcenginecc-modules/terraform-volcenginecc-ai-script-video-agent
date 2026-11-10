locals {
  deployment_enabled = local.auto_delivery || var.source_release != null
  deployment_config = !local.deployment_enabled ? null : merge(
    var.source_release == null ? {} : var.source_release,
    local.auto_delivery ? {
      bucket          = volcenginecc_tos_bucket.source[0].name
      object_key      = volcenginecc_tos_object.source[0].key
      source_url      = split("?", volcenginecc_tos_object.source[0].url)[0]
      payload_format  = "base64gzip_json_chunks"
      manifest_sha256 = sha256(local.source_manifest)
      sha256          = local.source_payload_sha256
    } : {},
    { role_name = volcenginecc_iam_role.app.role_name }
  )
  comfyui_runtime = var.comfyui == null ? null : {
    base_url       = var.comfyui.base_url
    api_key_file   = "/opt/ai-animation/secrets/comfyui_api_key"
    workflow       = try(jsondecode(var.comfyui.workflow_json), {})
    bindings       = { for name, binding in var.comfyui.bindings : name => binding if binding != null }
    output_node_id = var.comfyui.output_node_id
    output_index   = var.comfyui.output_index
    capabilities   = var.comfyui.capabilities
  }
  runtime_config = merge({
    video_provider = var.video_provider
    infrastructure = {
      region    = var.region
      role_trn  = volcenginecc_iam_role.app.trn
      role_name = volcenginecc_iam_role.app.role_name
      auth_mode = "ecs_instance_role"
    }
    storage = {
      auto_upload       = true
      backend           = "tos"
      bucket            = volcenginecc_tos_bucket.assets.name
      region            = var.region
      intranet_endpoint = volcenginecc_tos_bucket.assets.intranet_endpoint
      extranet_endpoint = volcenginecc_tos_bucket.assets.extranet_endpoint
      url_ttl_seconds   = var.media_url_ttl_seconds
    }
    models = {
      ark_base_url     = var.ark_base_url
      script_model_id  = var.script_model_id
      video_model_id   = var.video_model_id
      api_key_file     = "/opt/ai-animation/secrets/ark_api_key"
      project_override = true
    }
    worker = {
      queue_backend       = "postgresql"
      composition_workers = 1
    }
  }, local.comfyui_runtime == null ? {} : { comfyui = local.comfyui_runtime })

  cloud_init = "#cloud-config\n${jsonencode({
    package_update = true
    ssh_pwauth     = var.ecs_login_mode == "password"
    # Older VolcStack cloud-init calls the Ubuntu service by the sshd alias.
    # bootcmd runs before set-passwords, while runcmd would be too late.
    bootcmd = [
      ["install", "-d", "-m", "0755", "/run/sshd"],
      ["install", "-d", "-m", "0700", "/opt/ai-animation/secrets"],
      ["bash", "-c", "if [ -f /lib/systemd/system/ssh.service ] && [ ! -e /etc/systemd/system/sshd.service ]; then ln -s /lib/systemd/system/ssh.service /etc/systemd/system/sshd.service; fi; systemctl daemon-reload"],
      ["python3", "-c", <<-PYTHON
        import glob
        from pathlib import Path
        import yaml
        target = Path("/etc/cloud/cloud.cfg.d/zz-ai-animation-no-console.cfg")
        modules = None
        for name in ["/etc/cloud/cloud.cfg"] + sorted(glob.glob("/etc/cloud/cloud.cfg.d/*.cfg")):
            if Path(name) == target:
                continue
            config = yaml.safe_load(Path(name).read_text()) or {}
            if "cloud_final_modules" in config:
                modules = config["cloud_final_modules"]
        if modules is not None:
            modules = [module for module in modules if (module[0] if isinstance(module, list) else module).replace("_", "-") != "keys-to-console"]
            target.write_text(yaml.safe_dump({"cloud_final_modules": modules}))
        PYTHON
      ],
    ]
    # Modern versions support this flag; 20.3 also needs the module filter above.
    ssh      = { emit_keys_to_console = false }
    packages = ["postgresql-16", "nginx", "ffmpeg", "openssl", "python3-venv", "openssh-server"]
    write_files = concat([
      {
        path        = "/etc/ssh/sshd_config.d/00-ai-animation-login.conf"
        owner       = "root:root"
        permissions = "0644"
        content     = "PasswordAuthentication ${var.ecs_login_mode == "password" ? "yes" : "no"}\nPermitRootLogin ${var.ecs_login_mode == "password" ? "yes" : "prohibit-password"}\nKbdInteractiveAuthentication no\n"
      },
      {
        path        = "/opt/ai-animation/config/runtime.json"
        owner       = "root:root"
        permissions = "0644"
        encoding    = "gz+b64"
        content     = base64gzip(jsonencode(local.runtime_config))
      },
      {
        path        = "/opt/ai-animation/secrets/ark_api_key"
        owner       = "root:root"
        permissions = "0600"
        content     = "${trimspace(var.ark_api_key)}\n"
      },
      {
        path        = "/usr/local/sbin/ai-animation-prepare"
        owner       = "root:root"
        permissions = "0750"
        content     = <<-SCRIPT
          #!/bin/bash
          set -euo pipefail
          install -d -m 0755 /run/sshd
          install -d -m 0700 /opt/ai-animation/secrets
          install -d -m 0750 /opt/ai-animation/data /opt/ai-animation/work
          for name in postgres_password owner_password session_key; do
            if [ ! -s "/opt/ai-animation/secrets/$name" ]; then
              umask 077
              openssl rand -hex 32 > "/opt/ai-animation/secrets/$name"
            fi
          done
          # Disable Ubuntu's welcome page before enabling the application site.
          rm -f /etc/nginx/sites-enabled/default
          systemctl stop nginx.service
          SCRIPT
      },
      {
        path        = "/etc/systemd/system/ai-animation.service"
        owner       = "root:root"
        permissions = "0644"
        content     = <<-UNIT
          [Unit]
          Description=Install AI animation native application
          After=network-online.target
          Wants=network-online.target
          StartLimitIntervalSec=0
          ConditionPathExists=/opt/ai-animation/config/deployment.json

          [Service]
          Type=oneshot
          RemainAfterExit=yes
          WorkingDirectory=/opt/ai-animation
          ExecStart=/usr/local/sbin/ai-animation-bootstrap
          TimeoutStartSec=3600
          Restart=on-failure
          RestartSec=60
          UMask=0077

          [Install]
          WantedBy=multi-user.target
          UNIT
      }
      ], var.comfyui == null ? [] : [{
        path        = "/opt/ai-animation/secrets/comfyui_api_key"
        owner       = "root:root"
        permissions = "0600"
        content     = "${trimspace(var.comfyui_api_key)}\n"
      }], !local.deployment_enabled ? [] : [
      {
        path        = "/opt/ai-animation/config/deployment.json"
        owner       = "root:root"
        permissions = "0640"
        content     = jsonencode(local.deployment_config)
      },
      {
        path        = "/usr/local/lib/ai-animation-deploy.py"
        owner       = "root:root"
        permissions = "0640"
        encoding    = "gz+b64"
        content     = base64gzip(file("${path.module}/scripts/ecs-deploy.py"))
      },
      {
        path        = "/usr/local/sbin/ai-animation-bootstrap"
        owner       = "root:root"
        permissions = "0750"
        content     = file("${path.module}/scripts/ecs-bootstrap.sh")
      }
    ])
    runcmd = [
      ["/usr/local/sbin/ai-animation-prepare"],
      ["/usr/sbin/sshd", "-t"],
      ["systemctl", "daemon-reload"],
      ["systemctl", "enable", "ssh.service"],
      ["systemctl", "reload-or-restart", "ssh.service"],
      !local.deployment_enabled ? ["systemctl", "enable", "ai-animation.service"] : ["systemctl", "enable", "--now", "ai-animation.service"],
    ]
  })}\n"
}
