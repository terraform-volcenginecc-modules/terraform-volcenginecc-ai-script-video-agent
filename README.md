# AI Script and Video Creation Studio on Volcengine

This template uses Terraform and `volcengine/volcenginecc` to create networking, ECS, TOS, and an instance role on Volcengine. ECS downloads and installs the official ComfyUI frontend and backend. Custom nodes connect to a separate FastAPI adapter, PostgreSQL Worker, Ark models, FFmpeg, and private TOS. Users run project, script, video, and composition nodes on the official canvas and review storyboards, recover tasks, and preview films in the studio sidebar.

## 1. Business Background

Short video production involves story development, storyboard design, visual generation, and editing. Working directly with separate model APIs introduces several challenges:

- **Fragmented creative workflow**: scripts, prompts, clips, and final films are stored separately, making revisions difficult to track.
- **Inconsistent shots**: character appearance, clothing, positions, and actions can change between shots, or the next shot may repeat the previous action.
- **Difficult asynchronous task management**: cloud video generation takes time, and interrupted connections, moderation failures, or failed downloads can lead to duplicate submissions.
- **Incomplete asset retention**: temporary video URLs without persistent assets and task records make it difficult to resume editing or investigate failures.

This solution brings project settings, structured storyboards, model tasks, reference assets, video archiving, and film export into one studio. Users review shots before manually generating videos and can use the previous shot's out-point frame to help maintain continuity.

## 2. Solution Overview

- **Network and compute**: an isolated VPC, one `/24` application subnet, a security group, and an Ubuntu 24.04 ECS instance with an embedded public EIP. Nginx serves the frontend and reverse proxy; parameters control Web and SSH ingress.
- **Application and database**: the official ComfyUI frontend and Python backend run as a separate service. Custom nodes connect over HTTP to the business API and Worker. PostgreSQL 16 stores projects, shots, tasks, leases, and the backup queue. ComfyUI, business API, and database ports are not exposed directly.
- **Models**: Doubao 2.1 generates scripts and Seedance 2.0 generates videos by default. Projects can select other enabled models or inference endpoints. The template configures model IDs but does not create Ark endpoints or API keys.
- **Delivery**: Terraform publishes the adapter and installer as 16 KiB TOS chunks. ECS verifies them, downloads a pinned official ComfyUI release, verifies SHA256, installs its frontend package and CPU dependencies, and starts systemd services. No video model weights, npm build, or Docker runtime are required on ECS.
- **Assets and films**: private TOS stores final media, and browsers use short-lived signed URLs for direct playback and download. The local ECS directory is used for staging, frame extraction, and assembly; an independent upload queue persists media, and missing local cache files can be restored from TOS when needed.

```mermaid
flowchart TB
  User["Browser user"] -->|"HTTP 80 / HTTPS 443<br/>Configured ingress"| EIP["Embedded ECS EIP"]
  subgraph VPC["VPC / Application subnet"]
    subgraph ECS["Ubuntu 24.04 ECS / systemd"]
      EIP --> Nginx["Nginx / Login and WebSocket proxy"]
      Nginx -->|"Default UI / 8188"| Comfy["Official ComfyUI / Custom nodes"]
      Comfy -->|"Internal HTTP / 8000"| API["FastAPI adapter / Password sessions"]
      Nginx -->|"/studio-api/"| API
      API --> PG["Local PostgreSQL 16<br/>Projects / Tasks / Backup queue"]
      Worker["Python Worker"] <-->|"Claim tasks / Save results"| PG
      Worker --> FFmpeg["FFmpeg / Frame extraction and hard cuts"]
      FFmpeg --> Media["Local media directory"]
      Worker --> Media
      API -->|"Internal processing cache"| Media
    end
  end
  Worker <-->|"HTTPS scripts / Async video tasks"| Ark["Volcengine Ark<br/>Doubao / Seedance"]
  Media -->|"Independent upload queue / Instance role"| TOS["Private TOS media bucket"]
  API -->|"Return signed URL after authentication"| User
  User -->|"Play / Download"| TOS
  TOS -->|"Restore missing processing cache"| Media
  Source["TOS source chunks and manifest<br/>Public-read objects / SHA256 verification"] -->|"HTTPS download"| ECS
```

Creative workflow: **run project and script nodes → review storyboard JSON in the sidebar → run video nodes → run composition nodes → preview or download MP4**. See section 4 for usage. Repeated execution reuses persistent projects and tasks; failed or uncertain tasks require explicit recovery.

The default model IDs are `doubao-seed-2-1-pro-260628` and `doubao-seedance-2-0-260128`, with the Ark API at `https://ark.cn-beijing.volces.com/api/v3`. Confirm availability and model permissions in the target account before deployment. Generation and regeneration incur model usage charges; saving settings and previewing prompts do not invoke models.

## 3. IaC Design

### 3.1 Resource Inventory

Cloud resources use `volcengine/volcenginecc = 0.0.73`; Terraform must be `>= 1.5.7, < 2.0`. The destruction delay uses `hashicorp/time = 0.14.2` and `time_sleep`, without local command execution.

| Terraform Resource | Logical Name | Count | Purpose |
| --- | --- | ---: | --- |
| `volcenginecc_vpc_vpc` | `app` | 1 | Isolated VPC |
| `volcenginecc_vpc_subnet` | `app` | 1 | Application `/24` subnet |
| `volcenginecc_vpc_security_group` | `app` | 1 | SSH, HTTP, and HTTPS source restrictions |
| `volcenginecc_ecs_instance` | `app` | 1 | Pay-as-you-go CPU ECS, system disk, primary NIC, EIP, and role association |
| `volcenginecc_tos_bucket` | `assets` | 1 | Private media bucket with versioning enabled by default |
| `volcenginecc_tos_bucket_cors` | `assets` | 0 or 1 | CORS when `cors_allowed_origins` is nonempty |
| `volcenginecc_iam_policy` | `assets` | 1 | Listing and object read/write/delete permissions scoped to the media bucket |
| `volcenginecc_iam_role` | `app` | 1 | ECS trust and required runtime policies |
| `volcenginecc_iam_policy` | `delivery` | 0 or 1 | Read access to the specified private source archive when `source_release` is set |
| `volcenginecc_tos_bucket` | `source` | 0 or 1 | Separate source bucket for automatic delivery |
| `volcenginecc_tos_object` | `source_parts` | N | Automatic source chunks, each no larger than 16 KiB |
| `volcenginecc_tos_object` | `source` | 0 or 1 | Verification manifest published after all chunks |
| `time_sleep` | `eni_release_wait` | 1 | Delay after ECS deletion and before security group deletion; creates no cloud resource |

The current inputs use automatic delivery without CORS. The current source and frontend produce N = 14, for 23 cloud resources and 1 wait resource. N changes with package contents; use the execution plan and `source_delivery.chunk_count` for the actual count. The EIP, system disk, primary NIC, and role association are embedded ECS settings, not separate resources. The ECS installer creates PostgreSQL, database accounts, the Web login password, and application services. This project has no data sources or child modules; the deployer supplies the image, key pair, cloud project, and Ark model permissions.

Delivery modes:

| Configuration | Behavior |
| --- | --- |
| `auto_source_upload = true`, `source_release = null` | Default: Terraform uploads chunks and starts the application |
| Set `source_release` | Use an existing private TOS release archive; takes precedence over automatic upload |
| `auto_source_upload = false`, `source_release = null` | Create infrastructure and base host configuration without installing application source |

Both source and media buckets have private bucket ACLs. Only automatically delivered source chunks and manifests are public-read; they must contain no credentials or business media. The required `ark_api_key` is passed directly as a sensitive Terraform input. The installer writes it to `/opt/ai-animation/secrets/ark_api_key` on ECS with mode `0600`. The key is present in Terraform state, saved plans, and ECS user data; `sensitive` only masks normal display and does not remove the secret from those data stores. In password login mode, `ecs_password` is also present in state and plans.

### 3.2 Project Structure

```text
.
├── backend/
│   ├── app/                     # API, Worker, model adapters, media processing and backup
│   ├── comfyui_extension.py              # Official custom nodes and login protection
│   └── requirements.txt
├── frontend/
│   ├── comfyui-extension/    # Official ComfyUI sidebar extension
│   ├── login/
│   └── nginx/
├── scripts/
│   ├── package-release.py       # Create a complete release bundle
│   ├── deploy-local.py          # Credential file injection and complete Terraform apply
│   ├── install-comfyui.py       # Verify official source and install CPU dependencies and extensions
│   ├── comfyui-release.json
│   ├── comfyui-requirements.txt # Pinned studio Python dependencies
│   ├── ecs-bootstrap.sh         # ECS installation entry point
│   ├── ecs-deploy.py            # Download, combine and verify source
│   ├── ecs-native.py            # Install database and systemd services
│   └── ecs-public-web.py        # Configure Nginx and test HTTPS
├── main.tf                      # Networking, ECS, media bucket and IAM
├── delivery.tf                  # Source encoding, chunks and manifest
├── bootstrap.tf                 # Runtime configuration and cloud-init
├── variables.tf
├── outputs.tf
├── versions.tf
├── providers.tf                 # Provider configuration
├── terraform.tfvars             # Local deployment parameters
├── compose.yaml                 # Optional local container setup
├── compose.dev.yaml
├── docs/comfyui-video-provider.md # Optional remote GPU video service configuration
├── tests/                       # Local regression tests and fixtures
├── README.md                    # English documentation
└── README.zh-CN.md               # Chinese documentation
```

This release includes custom nodes, a login page, a pinned component manifest, and `terraform.tfvars`. Replace environment parameters before deployment. The official frontend is installed with ComfyUI without an npm build. Parameter files, state, plans, secrets, runtime configuration, database files, and media are excluded from source delivery.

All Terraform resources are defined in the root module. The bootstrap chain is `bootstrap.tf` → `scripts/ecs-bootstrap.sh` → `scripts/ecs-deploy.py` → `scripts/ecs-native.py`. Official ComfyUI is downloaded and installed separately. This project maintains business adapters in `frontend/comfyui-extension/`, `backend/comfyui_extension.py`, and `backend/app/`.

### 3.3 Key Code Examples

`delivery.tf` encodes runtime files and splits the payload into small objects. The manifest records the encoded size, chunk count, and checksums. Key fields of the manifest resource are shown below:

```hcl
resource "volcenginecc_tos_object" "source" {
  count      = local.auto_delivery ? 1 : 0
  bucket     = volcenginecc_tos_bucket.source[0].name
  key        = "releases/${local.source_payload_sha256}/manifest.json"
  content    = local.source_manifest
  public_acl = "public-read"
  depends_on = [volcenginecc_tos_object.source_parts]
  # The full configuration also sets content type, encryption, tags and size preconditions.
}
```

`bootstrap.tf` writes the manifest location, manifest SHA256, and source SHA256 into the deployment configuration. ECS downloads chunks in order and verifies the manifest, chunk lengths, combined payload, and decompressed source before installing the application. Encoded content is limited to 10 MiB and decompressed JSON to 100 MiB. Legacy single-object delivery and private `.tar.gz` releases remain supported.

Creation dependencies are `security group → time_sleep.eni_release_wait → ECS`; destruction runs in reverse, with a default 120-second delay to allow asynchronous NIC detachment. No creation delay is configured, and destruction does not invoke Python or shell commands. The wait resource must have been applied to state before a later destroy can execute it.

Chunks use Terraform's default parallelism. Changing the source delivery mode adds or removes the corresponding source bucket, objects, and read policy; review the execution plan before deployment. Section 4 covers deployment and verification.

## 4. Usage

This project supports cloud deployment through InfraManager or local deployment using the downloaded template. Cloud deployment is recommended: you do not need to prepare a Terraform runtime, and you can create and manage resources by entering parameters in the platform.

### 4.1 Cloud Deployment

InfraManager is Volcengine's managed Terraform workspace. For more information, see the [InfraManager product introduction](https://docs.volcengine.com/docs/infrastructure-management/product-introduction?lang=zh).

#### 4.1.1 Prepare Permissions

The stack execution identity needs CloudControl access and permissions to manage and associate VPCs, subnets, security groups, ECS instances, disks, NICs, EIPs, TOS resources, and IAM roles/policies, including passing a role to ECS. The application uses the instance role created by the template to access specified buckets or objects; deployment and runtime identities serve different purposes.

Prepare an Ubuntu 24.04 amd64 image, an available CPU ECS instance type, EIP and disk quotas, and a globally unique media bucket name in the target region. A starting capacity of 4 vCPU / 8 GiB is recommended; actual requirements depend on generation concurrency, video archiving, and FFmpeg load.

Enable the script and video models in the [Volcengine Ark console](https://console.volcengine.com/ark) and create an API key authorized to invoke them. An Ark key differs from cloud account AK/SK credentials, model IDs, and `ep-...` endpoint IDs. Supply the required `ark_api_key` as a sensitive platform input. See section 3.1 for secret-file and state-storage details.

On first boot, ECS installs dependencies from public sources. It downloads the ComfyUI source from GitHub, verifies its SHA-256, and retries or uses GitHub's direct archive URL if needed. CPU PyTorch uses the SJTU mirror with the official CPU index as fallback. Other Python packages use the Volcengine mirror with official PyPI as fallback. ECS also needs access to TOS, Ubuntu repositories, and Ark HTTPS APIs.

The studio keeps the pinned official frontend and node runtime. The bundled ComfyUI sample workflows, sample media, and embedded node docs are omitted. Studio workflows still load onto the canvas from the sidebar. No dependency package needs to be uploaded before deployment.

#### 4.1.2 Select the Template

Open the [InfraManager console](https://console.volcengine.com/iac), go to **Template Management > Public Templates**, and search for “AI Script and Video Creation Studio”.

This search path applies after publication, which this document does not confirm. Custom imports must preserve root Terraform files, `scripts/comfyui-release.json`, installers, backend source, login files, Nginx configuration, and `frontend/comfyui-extension/`. Importing only `.tf` files cannot install the application.

On a release machine with Python 3, run:

```bash
python3 scripts/package-release.py --version v2026.11.10-native
```

Run the packaging script from the project root with Python 3. It packages custom extensions and installers without an npm build, then writes the archive, SHA256 checksum, metadata, and private source-reference example. Use a new version after changes. The script does not upload files or create cloud resources. Keep generated release files outside the source tree when testing.

The source archive excludes local `terraform.tfvars`, state, plans, `.env`, secrets, and media data, and includes a per-file integrity manifest for the installer. It is an ECS application source package, not a replacement for the complete Terraform project required by InfraManager. Supply deployment parameters separately as described in section 4.2.1.

#### 4.1.3 Create a Stack

Select the template and click **Create Stack**. Enter the stack information and template parameters for your requirements. Create and review the execution plan, then click **Deploy** after confirming the changes are as expected.

Parameter details are in section 4.2.1. Required inputs are `zone_id`, `image_id`, `instance_type`, `bucket_name`, `ssh_allowed_cidrs`, `ark_api_key`, and the ECS credentials for the selected login mode.

For key-pair mode, provide an existing `ssh_key_pair_name` and leave `ecs_password` empty. For password mode, leave the key-pair name empty and supply a real `ecs_password` as a sensitive parameter. The current local parameter file uses password mode. Public ports 80/443 open only when `web_allowed_cidrs` is configured; an empty set retains SSH tunnel access.

Review chunk counts, bucket names, allowed CIDRs, role permissions, and any deletions or replacements in the plan before deployment. A successful Terraform run or stack operation confirms resource creation; application download and installation may still be running.

#### 4.1.4 Verify the Deployment

Confirm that the deployment status is successful on the stack details page, then review the template outputs:

| Output | Purpose |
| --- | --- |
| `infrastructure` | VPC, subnet, security group, and ECS identifiers, subnet CIDR, and public IP |
| `asset_storage` | Private media bucket, internal/external endpoints, and ECS role TRN |
| `runtime_config` | Non-secret model, storage, role, and Worker settings |
| `application_access` | Public URLs, SSH tunnel, configuration/certificate paths, status and log commands |
| `source_delivery` | Delivery mode, bucket, object key, source SHA256, public manifest URL, and chunk count |

Outputs do not return the login password or API key, but they contain resource locations and runtime settings. Default automatic delivery creates N + 9 cloud resources and 1 wait resource; check resource types and states on the Resources tab, and see section 4.2.3 for counts. Public URL outputs are `null` when Web sources are empty.

Locate ECS using `infrastructure.instance_id`. Run these commands in its console terminal or an SSH session:

```bash
sudo cloud-init status
sudo cat /opt/ai-animation/deployment-status.json
sudo systemctl is-active ai-animation-api.service ai-animation-worker.service ai-animation-comfyui.service nginx.service
curl --fail --silent --show-error --max-time 15 http://127.0.0.1:8080/studio-api/health
```

When ready, the deployment status `phase` is `ready`, the four persistent services are `active`, and the health endpoint returns `{"status":"ok"}`. A missing or malformed key fails installation during `configuring_secret`. A nonempty key file does not verify model permissions through a real invocation.

If installation fails, inspect status and logs:

```bash
sudo systemctl status ai-animation.service ai-animation-api.service ai-animation-worker.service ai-animation-comfyui.service nginx.service --no-pager
sudo journalctl -u ai-animation.service -u ai-animation-api.service -u ai-animation-worker.service -u ai-animation-comfyui.service -n 100 --no-pager
```

To rotate the Ark key, update the sensitive platform input and review the Terraform plan. Updating ECS user data does not replay cloud-init on an existing instance; if the instance is not replaced, update `/opt/ai-animation/secrets/ark_api_key` through a controlled operations path and restart the API and worker services. Keep secrets out of command arguments and logs.

Read the studio login password in a controlled terminal:

```bash
sudo cat /opt/ai-animation/secrets/owner_password
```

Open `application_access.public_http_url` or `public_https_url` and sign in with that password. The Web password differs from ECS login credentials. HTTPS initially uses a self-signed test certificate valid for one year. For trusted domain access, replace `/etc/nginx/ssl/ai-animation/tls.crt` and `tls.key`, then check and reload Nginx.

If public Web ingress is disabled, use `application_access.ssh_tunnel_command` and open `http://127.0.0.1:8080`. Choose a different local forwarding port if another application already uses it.

Official canvas usage and acceptance checks:

1. Open the studio sidebar (“视频工作室”), select an existing project, or load a new project onto the canvas (“加载新项目到画布”). Enter the project name, creation round, requirements, and output settings, then click the official Run button. After script generation, refresh sidebar status (“刷新状态”) and select the project.

2. Review or edit storyboard shots (“审阅分镜”) and save them directly in the sidebar. For an existing project without a script, load the script workflow (“加载剧本到画布”) and click Run.

3. After review, load the video workflow (“加载视频到画布”) and click Run. Refresh status afterward; recover failed or uncertain tasks in task history (“任务记录”).

4. Once all shot assets are available, load the composition workflow (“加载拼接到画布”) and click Run. Refresh status to preview or download the MP4 in the final-film section (“拼接成片”).

5. Use the browser network panel to confirm that playback and download request signed TOS URLs directly. Refresh previews to renew expired URLs. Retry failed archives separately and confirm reuse of the existing asset and Ark Task ID.

6. Restart ECS when appropriate and confirm service startup and access to projects, tasks, and TOS media. This does not replace database backups or real model acceptance checks; an accepted request does not prove video success.

The same project name and creation round reuse the project and its tasks. Changed creation inputs require a new round. An existing project ID takes precedence over other node inputs. Interrupting canvas execution stops node waiting only; accepted business tasks persist, and uncertain tasks must not be resubmitted automatically.

Archive test projects when they are no longer needed. Archiving retains media and does not release storage. See section 4.2.5 for full data cleanup.

### 4.2 Local Deployment

You can also download and run the template locally by following the steps below.

Run the following Terraform commands from this project's root directory on macOS, Linux, or WSL to deploy cloud resources. Run ECS status checks and secret configuration commands in the ECS terminal.

With an existing AK-mode configuration in `~/.volcengine/config.json`, the local deployment script can inject credential files and run init, validate, a complete plan, and one apply. Password mode also requires an ECS password file:

```bash
python3 scripts/deploy-local.py \
  --ark-key-file "/absolute/path/to/ark-key-file" \
  --ecs-password-file "/absolute/path/to/ecs-password-file"
```

The script stores state, plans, logs, and outputs outside the source tree in `/tmp/ComfyUI完整部署` with directory mode `0700`. Use `--work-dir` for another external directory and retain it for future updates and destruction. The script stops when a plan contains deletion or replacement and preserves state without automatic retries after apply failure.

#### 4.2.1 Prepare the Environment

**Base environment**:

Install Terraform `>= 1.5.7, < 2.0` and prepare network access to Volcengine APIs. Packaging requires Python 3 only; the official ComfyUI frontend is installed on ECS. Provider versions are pinned in `versions.tf`, with configuration in `providers.tf`; `terraform init` generates `.terraform.lock.hcl`.

**Services and quotas**:

Prepare deployment permissions, the image, key pair, ECS/EIP/disk quotas, and an Ark key for enabled models in the console as described in section 4.1.1. Both deployment paths require the same cloud-resource permissions.

**Credential injection** (environment variables are recommended; do not store credentials in code or `tfvars`):

```bash
export VOLCENGINE_ACCESS_KEY="your_access_key"
export VOLCENGINE_SECRET_KEY="your_secret_key"
export VOLCENGINE_REGION="cn-beijing"
```

Temporary credentials also require `VOLCENGINE_SESSION_TOKEN`. This project's provider explicitly uses the `region` input, so the environment variable does not override it; keep both values consistent.

**Sensitive business variables**: `ark_api_key` is a required sensitive field used to call Ark models. `ecs_password` is sensitive and applies only to ECS password login. Inject them through environment variables instead of storing them in `terraform.tfvars`:

```bash
export TF_VAR_ark_api_key="<your_ark_api_key>"
# Only for ECS password login.
export TF_VAR_ecs_password="<strong-password>"
```

Replace placeholders with real values and protect terminal history. Values assigned in `terraform.tfvars` take precedence over `TF_VAR_*`. Remove any file assignment for `ark_api_key` before environment injection. For password login, also remove the file assignment for `ecs_password`, including `null`. For key-pair login, keep `ecs_password = null` in the file and do not export a password.

**Key parameters**:

Review the local `terraform.tfvars` and update values for the target account. When using a complete project bundle without this file, create it from the parameters below. Defaults come from `variables.tf` and may differ from the local environment's configured values:

| Parameter | Default / input | Description |
| --- | --- | --- |
| `region` / `zone_id` | `cn-beijing` / required | Region and an availability zone with capacity |
| `project_name` / `name_prefix` | `default` / `ai-animation` | Cloud project and resource name prefix |
| `tags` | `{}` | Additional tags |
| `vpc_cidr` | `10.42.0.0/16` | Derives the first nonzero `/24` subnet, `10.42.1.0/24` by default |
| `image_id` | Required | Ubuntu 24.04 amd64 image with cloud-init enabled |
| `instance_type` | Required | Available CPU ECS type; start with 4 vCPU / 8 GiB |
| `ecs_login_mode` | `key_pair` | `key_pair` or `password` |
| `ssh_key_pair_name` | `null` | Existing key-pair name required in key-pair mode |
| `ecs_password` | `null`, sensitive | Real password required in password mode; leave empty for key-pair login |
| `ssh_allowed_cidrs` | Required | Nonempty set of administrator IPv4 CIDRs |
| `web_allowed_cidrs` | `[]` | Allowed sources for 80/443; empty disables public Web ingress |
| `system_disk_size_gib` / `system_disk_type` | `100` / `ESSD_FlexPL` | System disk size and type; at least 40 GiB |
| `delete_system_disk_with_instance` | `true` | Delete the system disk with ECS; set to `false` to retain it and continue billing |
| `deletion_protection` | `false` | Cloud-side ECS deletion protection |
| `eni_release_wait_seconds` | `120` | Wait for NIC release after ECS deletion; 0–900 seconds |
| `public_bandwidth_mbps` | `10` | Bandwidth cap for the traffic-billed EIP |
| `bucket_name` | Required | Globally unique private media bucket name |
| `bucket_versioning` | `Enabled` | Media bucket versioning; also supports `Suspended` |
| `cors_allowed_origins` | `[]` | Exact browser origins; empty skips CORS creation |
| `auto_source_upload` | `true` | Terraform automatically uploads source chunks and the manifest |
| `source_release` | `null` | Existing private source archive reference; overrides automatic upload |
| `ark_api_key` | Required, sensitive | Ark API key written directly to the backend secret file and stored in Terraform state |
| `ark_base_url` | `https://ark.cn-beijing.volces.com/api/v3` | Ark API base URL |
| `script_model_id` | `doubao-seed-2-1-pro-260628` | Default script model; can be overridden per project |
| `video_model_id` | `doubao-seedance-2-0-260128` | Default video model; can be overridden per project |
| `video_provider` | `ark` | 可选 `comfyui` 远程视频服务；剧本继续使用 Ark |
| `comfyui` | `null` | 选择 ComfyUI 时必填：真实 API 工作流、节点映射、能力与远程地址 |
| `comfyui_api_key` | 空，敏感 | 选择 ComfyUI 时必填的 Bearer 密钥，直接写入 ECS 密钥文件 |
| `media_url_ttl_seconds` | `900` | Signed media URL lifetime, an integer from 60–3600 seconds; shortened when the instance-role credential expires sooner |

Login credentials are mutually exclusive: provide only the key-pair name in key-pair mode and only the password in password mode. Supply `ark_api_key` through a sensitive platform input or `TF_VAR_ark_api_key`. Automatically loaded `terraform.tfvars` values take precedence over `TF_VAR_*`; do not assign a variable in the parameter file when injecting the same variable through the environment.

CPU ECS runs the official ComfyUI interface and API nodes; Ark performs default model computation. Deployment and usage are described in this section. `video_provider = "comfyui"` selects an additional remote GPU video service whose real workflow contract is documented in [the provider guide](docs/comfyui-video-provider.md).

#### 4.2.2 Scenario-based Deployment Examples

The official frontend is installed with pinned ComfyUI dependencies. Updated custom sidebar files can be delivered directly; no npm build is needed.

Initialize and validate:

```bash
terraform init
terraform fmt -check -recursive
terraform validate
```

The following scenarios target independent new environments. First supply account-specific base parameters. All resource identifiers below are placeholders:

```hcl
region            = "cn-beijing"
zone_id           = "cn-beijing-a"
image_id          = "image-REPLACE"
instance_type     = "ecs.g3i.xlarge"
bucket_name       = "your-unique-media-bucket"
ssh_allowed_cidrs = ["203.0.113.10/32"]
web_allowed_cidrs = ["203.0.113.10/32"]
```

Replace the image, instance type, bucket name, and source addresses, and confirm capacity in the selected zone. Inject the Ark key as described in section 4.2.1.

**Scenario 1: automatic source upload and ECS key-pair login.** After filling in all required parameters, set the following in `terraform.tfvars`:

```hcl
auto_source_upload = true
source_release     = null
ecs_login_mode     = "key_pair"
ssh_key_pair_name  = "your-existing-key-pair"
ecs_password       = null
```

```bash
terraform plan -out=自动交付.tfplan
terraform show 自动交付.tfplan
terraform apply 自动交付.tfplan
```

One apply creates infrastructure, uploads chunks of the current source, and triggers ECS installation. Commands use Terraform's default parallelism. Do not reuse an old plan created before changing the template.

**Scenario 2: existing private release archive and ECS password login.** Create a source archive with the release script, upload it to an existing private delivery bucket, and use the full SHA256 from the script output:

```hcl
ecs_login_mode    = "password"
ssh_key_pair_name = null
# Supply ecs_password through TF_VAR_ecs_password or a sensitive platform parameter.

source_release = {
  bucket     = "your-existing-delivery-bucket"
  region     = "cn-beijing"
  endpoint   = "https://tos-cn-beijing.volces.com"
  object_key = "releases/<version>/<sha256>/<archive>.tar.gz"
  sha256     = "<64-lowercase-hex-sha256>"
}
```

```bash
terraform plan -out=私有交付.tfplan
terraform show 私有交付.tfplan
terraform apply 私有交付.tfplan
```

This mode does not create an automatic source bucket or chunks. It adds a private-archive read policy and uses the instance role to read the specified object. Use `source_release.version_id` to pin an object version; otherwise use an immutable object key. The password must be supplied through an environment variable or sensitive platform input.

**Scenario 3: infrastructure only.** Set `auto_source_upload = false` and `source_release = null`, while retaining the required Ark key and valid ECS login settings:

```bash
terraform plan -out=基础设施.tfplan
terraform show 基础设施.tfplan
terraform apply 基础设施.tfplan
```

This mode creates networking, ECS, the media bucket, and the role, and writes base host configuration, but it does not install the studio. There is no Web health check or model workflow. If source delivery is enabled later, verify that ECS installation is actually triggered.

All modes require direct `ark_api_key` input. When changing delivery, login, or bootstrap settings in an existing environment, review deletion and replacement actions. Updating ECS user data does not automatically replay cloud-init on an existing host.

#### 4.2.3 Expected Deployment Results

The base configuration creates 7 cloud resources, plus 1 `time_sleep` resource:

| Scenario | Cloud Resources | Total Terraform-managed Objects |
| --- | ---: | ---: |
| Automatic delivery, no CORS | N + 9; when current N = 14, the result is 23 | N + 10; currently 24 |
| Existing private release, no CORS | 8 | 9 |
| Infrastructure only, no CORS | 7 | 8 |

Each scenario adds 1 CORS resource when `cors_allowed_origins` is nonempty. N changes with source and frontend package contents. ECS, EIP, and disks incur compute and network charges; TOS incurs storage, request, and traffic charges; Ark generation and regeneration are billed by model usage.

View outputs with:

```bash
terraform output
terraform output -json infrastructure
terraform output -json application_access
terraform output -json source_delivery
```

The complete output descriptions are in section 4.1.4. In infrastructure-only mode, `application_access.source_deploy_enabled` is `false` and `source_delivery.mode` is `infrastructure_only`; access URLs in outputs do not mean the studio is installed.

#### 4.2.4 Post-deployment Verification

Use `infrastructure.instance_id` and `application_access` from section 4.2.3 to locate ECS and the access endpoint. For automatic and private-release delivery, follow section 4.1.4 to verify the studio. For private releases, also confirm that the `source_delivery` bucket, object key, and SHA256 match the package.

For infrastructure-only mode, confirm in the ECS console that the instance is running and attached to the expected VPC, subnet, security group, and role. Connect by SSH, verify that `cloud-init status` completes and `/opt/ai-animation/config/runtime.json` exists; application services are not expected to be `active`. In every mode, confirm that the security group permits only configured sources on 22/80/443 and that database and API ports remain private.

With configuration unchanged, check the plan:

```bash
terraform plan -detailed-exitcode
```

Exit code `0` means no changes, `2` means changes are planned, and `1` means an error. Investigate actual cloud resources and inputs before acting on a difference. If creation has an uncertain result or an object already exists, query the resource first to avoid duplicate creation. For `Resource Not Found`, compare the stack, console resources, and state.

For runtime failures, inspect `deployment-status.json` and the service logs from section 4.1.4 first. For download failures, inspect source delivery; for model failures, inspect the key, model permissions, and task history; for media playback failures, inspect upload status and the signed URL. If a business task has an uncertain result, query its existing Ark Task ID before resubmission to avoid duplicate charges.

#### 4.2.5 Destroy Resources

This project supports Terraform destruction of managed resources, but nonempty TOS buckets must be emptied manually first. Before destruction, back up PostgreSQL, media, secrets, and runtime configuration. Confirm that the directory, workspace, parameters, and credentials belong to the target environment.

If ECS deletion protection is enabled, disable and apply the change first. To retain the system disk, set `delete_system_disk_with_instance = false`, review and apply the change, and confirm that it does not replace ECS. A destroy operation does not first apply a pending disk-policy change.

After confirming that required data is backed up outside this stack, use the TOS console to remove all objects, historical versions, and delete markers from the media bucket. Check the automatic source bucket for remaining objects as well. Deleting only current versions does not empty a versioned bucket. Then review and execute destruction:

```bash
terraform plan -destroy
terraform destroy
terraform state list
```

> **Important: destruction does not empty the media bucket automatically.** If the bucket still contains runtime media, historical versions, delete markers, or incomplete multipart uploads, deletion of `ai-script-video-assets` returns `ResourceConflict` or `BucketNotEmpty`, leaving the stack in `DESTROY_FAILED`. This failure requires manual cleanup and is not a transient condition that resolves by waiting. In the TOS console, remove all of the data listed above and confirm that objects, historical versions, delete markers, and incomplete multipart uploads are all at `0`. Then run destroy again in InfraManager or rerun `terraform destroy` locally. Repeating destroy without cleanup is not guaranteed to succeed.

The media bucket in the current `main.tf` has `prevent_destroy = false`, and the source bucket has no such protection. Do not rely on Terraform to prevent bucket deletion. Runtime media uploads are not separate Terraform resources. Nonempty buckets, historical versions, or delete markers may block deletion and require handling according to the agreed retention plan. Having a backup does not mean its bucket can be deleted.

- `delete_system_disk_with_instance = true` deletes the system disk with ECS by default, including its PostgreSQL data, local media, secrets, and runtime configuration. Back up required data before destruction. Set it to `false` to retain the disk and continue billing.
- For an existing ECS instance, changing this setting in `terraform.tfvars` alone does not update the cloud disk policy. Review `terraform plan` and apply the change before destruction; verify that ECS is updated in place without replacement. A destroy operation does not apply a pending disk-policy change. If ECS has already been deleted, handle its retained disk separately instead of running an apply that would recreate the instance.
- Automatically delivered source buckets and objects are managed by the stack. Existing private release archives, existing ECS key pairs, and manually created Ark keys are not automatically deleted with it.
- The default NIC release delay is 120 seconds and does not guarantee that cloud detachment has finished. If ECS is already deleted but destruction fails, check NIC and security group associations before continuing destroy. Do not run a full apply that would recreate ECS merely to add a wait resource.
- The delay uses the value already applied to state. Review and apply changes before they can affect a later destroy. If ECS deletion protection is enabled, disable it through a reviewed plan first.
- After completion, check state, remaining console resources, and charges. Empty state does not mean retained disks or external resources have been removed.

### 4.3 Creation and Task Recovery

- Select the script and video models on the canvas. Start a new round after changing creation inputs. The Ark account needs permission to use the selected model IDs or endpoints.
- The sidebar pauses storyboard editing and duplicate generation while a project has an active task. Editing a shot with an existing video clears the current shot video and final film associations. Archived files remain in TOS.
- Recover failed or uncertain tasks in task history. If an Ark Task ID exists, recovery resumes polling and archiving. If the submission result is uncertain and no ID was recorded, check Ark first.
- FFmpeg joins shot videos into an MP4. Preview or download it in the final film section and check shot order, image, and audio.

### 4.4 Asset Backup and Operational Checks

Cloud deployment enables `storage.auto_upload=true` by default, and private TOS is the final media store. The bucket comes from `bucket_name`. Uploads cover archived current and historical shot videos, final films, model ending frames, extracted reference frames, uploaded images, and frame-selection preview videos.

Objects use the `media/<project_id>/<asset_id>/` prefix, private ACL, AES256 encryption, and SDK CRC checks. After upload, a HEAD request verifies the size and SHA256 metadata. The upload queue is independent of model tasks: failures retry only upload and do not regenerate video. Retry intervals grow from 30 seconds to at most 30 minutes, and interrupted Worker leases can be reclaimed after expiry. The interface shows generation and upload status separately and enables assembly and download only after upload succeeds.

Media becomes available for preview or assembly only after it is persisted. After authentication, the backend issues a short-lived TOS URL and the browser connects directly for playback and download. The default lifetime is 900 seconds and can be changed with `media_url_ttl_seconds`; the frontend refreshes expired URLs. Treat signed URLs as temporary access credentials while they remain valid.

When frame extraction or assembly needs a local file, the backend can download a missing cache file from TOS. This requires the database asset record and TOS object to remain available. TOS persists media only; it does not back up the project database, secrets, or runtime configuration. ECS data locations:

| Data | Path |
| --- | --- |
| PostgreSQL | `/var/lib/postgresql/16/main` |
| Media | `/opt/ai-animation/work/media` |
| Runtime configuration | `/opt/ai-animation/config/runtime.json` |
| Secrets | `/opt/ai-animation/secrets/` |
| Studio login password | `/opt/ai-animation/secrets/owner_password` |
| Installation status | `/opt/ai-animation/deployment-status.json` |

Check runtime status on ECS:

```bash
sudo systemctl is-active ai-animation-api.service ai-animation-worker.service ai-animation-comfyui.service nginx.service
curl --fail --silent --show-error --max-time 15 http://127.0.0.1:8080/studio-api/health
sudo journalctl -u ai-animation-worker.service -n 100 --no-pager
```

Confirm active services and `{"status":"ok"}` from the health endpoint, then inspect backup status in the interface and Worker logs. PostgreSQL advisory locks serialize database migrations. The current schema version is `6`, including task error details and the independent media backup queue.

The installer generates the studio login password randomly and stores it in the ECS `owner_password` file, not in the database. “Local machine” in the interface means the ECS server. The API loads the password through systemd credentials, and a normal restart does not regenerate it. To change the password on ECS:

```bash
sudoedit /opt/ai-animation/secrets/owner_password
sudo systemctl restart ai-animation-api.service
```

Replace the file contents with the new password and preserve its restricted permissions. Changing the password does not proactively revoke all existing sessions; rotate the session key as well when old sessions must be invalidated. The Ark key is likewise used only by the server and is neither stored nor displayed by the browser.

To pause business processing, run `sudo systemctl stop ai-animation-api.service ai-animation-worker.service` on ECS and use the corresponding `start` command to resume. Pausing does not delete tasks or media, and submitted Ark tasks may continue running and incurring charges.

Verify model permissions, application health, and data recovery in the target environment using section 4.1.4.

## 5. Summary

This template brings scripts, storyboards, video generation, and final-film export into one studio. Terraform manages cloud resources, systemd runs the application, and private TOS persists media. It is intended for demonstrations, functional validation, and small personal studios. The application and database run on one ECS instance; multiple replicas, high availability, multi-user authorization, public sharing, monitoring/alerting, and cross-region disaster recovery are not implemented.

- Reference frames and continuity prompts help connect shots but cannot guarantee consistent characters, actions, or voices. AI-generated people may also trigger cloud privacy or copyright moderation; changing text does not guarantee acceptance.
- A selected reference mode does not automatically fall back to text-only generation after a moderation failure. Users review the error and decide what to do next.
- Automatic sequential generation, dissolves, audio crossfades, precise ASR/TTS subtitles, local video/audio uploads for Ark references, and public callback endpoints are not supported.
- The website uses a studio password. Public deployment requires appropriate source restrictions and trusted HTTPS; self-signed certificates are for testing. The model key must be supplied directly through a Terraform input and is stored in the backend secret file; it is not configured or displayed in the website.
- Automatic delivery exposes runtime source code publicly. Manage state, plans, source delivery buckets, media buckets, and retained disks separately. Media backup is not a complete application backup and does not replace recovery drills.
