# openshift-playbook

Deploy a GPU-accelerated LLM inference endpoint on ROSA HCP with a single command.

One playbook takes you from an empty AWS account to a live, autoscaling model endpoint
backed by NVIDIA GPUs — handling cluster creation, operators, GPU drivers, and model
serving. Re-run it safely at any time; it only changes what's needed.

## What You Get

```
============================================================
  mistral-7b is ready
============================================================

  Endpoint: https://mistral-7b.apps.rosa.my-cluster.p3.openshiftapps.com
  Model:    mistral-7b
  GPUs:     1x g6.xlarge (NVIDIA L4)

  Test:
    curl -sk https://mistral-7b.apps.rosa.my-cluster.p3.openshiftapps.com/v1/chat/completions \
      -H 'Content-Type: application/json' \
      -d '{"model":"mistral-7b","messages":[{"role":"user","content":"Hello"}]}'

============================================================
```

An OpenAI-compatible `/v1/chat/completions` endpoint, running on a dedicated GPU node
with Knative autoscaling. Point any client, SDK, or benchmark tool at the URL.

## Quickstart

```bash
git clone <repo-url> && cd openshift-playbook
ansible-galaxy collection install -r requirements.yml
ansible-vault create inventory/group_vars/all/vault.yml   # add OCM token + registry creds
rh-aws-saml-login                                          # authenticate AWS
ansible-playbook playbooks/bootstrap.yml --ask-vault-pass  # auto-discover infrastructure
ansible-playbook playbooks/llm-endpoint.yml --ask-vault-pass
```

Already have a cluster? Skip straight to the workload:

```bash
ansible-playbook playbooks/llm-endpoint-on-cluster.yml --ask-vault-pass
```

## Scenarios

By default, the playbook deploys **Mistral-7B** (quantized W4A16) on a single L4 GPU — fast
to start, cheap to run, and good for development. Pass a scenario file to switch model,
GPU type, or scale — no code changes:

```bash
ansible-playbook playbooks/llm-endpoint.yml --ask-vault-pass \
  -e @scenarios/mistral-24b-benchmark.yml
```

| Scenario | Model | GPUs | What it's for |
|----------|-------|------|---------------|
| *(default)* | Mistral-7B (W4A16) | 1x L4 | Development and light usage |
| `mistral-24b.yml` | Mistral-Small-24B (W4A16) | 1x L4 | Higher quality, single GPU |
| `mistral-24b-demo.yml` | Mistral-Small-24B (W4A16) | 1x L4 (always on) | Demos — no autoscaling |
| `mistral-24b-large.yml` | Mistral-Small-24B (FP16) | 1-4x L40S | Full precision, 8k context |
| `mistral-24b-benchmark.yml` | Mistral-Small-24B (W4A16) | 4x L40S | Bulk throughput, 32k context |
| `llama-70b-benchmark.yml` | Llama-3.1-70B (FP8) | 2× g6e.12xlarge (8 L40S) | TP=2, 4 replicas, 16k context |
| `deepseek-r1-70b-benchmark.yml` | DeepSeek-R1-Distill-70B (FP8) | 2× g6e.12xlarge (8 L40S) | TP=2, 4 replicas, 16k context |
| `mistral-24b-llmd-benchmark.yml` | Mistral-Small-24B (W4A16) | 2+4 L40S | Disaggregated prefill/decode |

Create your own:

```yaml
# scenarios/my-scenario.yml
model_name: my-model
model_storage_uri: "hf://org/model-name"
gpu_instance_type: g6e.2xlarge
gpu_pool_min_replicas: 2
gpu_pool_max_replicas: 8
vllm_max_model_len: 8192
```

## Operations

```bash
# Full deploy (cluster + workload)
ansible-playbook playbooks/llm-endpoint.yml --ask-vault-pass

# Workload only (existing cluster)
ansible-playbook playbooks/llm-endpoint-on-cluster.yml --ask-vault-pass

# Disaggregated prefill/decode (llm-d)
ansible-playbook playbooks/llm-endpoint-llmd.yml --ask-vault-pass

# Scale GPUs to idle (min replicas, no pods)
ansible-playbook playbooks/gpu-scale.yml -e gpu_state=idle --ask-vault-pass

# Scale GPUs back to active
ansible-playbook playbooks/gpu-scale.yml -e gpu_state=active -e @scenarios/mistral-24b-benchmark.yml --ask-vault-pass

# Teardown one scenario
ansible-playbook playbooks/llm-endpoint-teardown.yml -e @scenarios/llama-70b-benchmark.yml --ask-vault-pass

# Teardown all workloads (keeps cluster)
ansible-playbook playbooks/llm-endpoint-teardown.yml --ask-vault-pass

# Teardown everything including cluster
ansible-playbook playbooks/llm-endpoint-teardown.yml --ask-vault-pass -e teardown_cluster=true

# Preflight check (validates everything, changes nothing)
ansible-playbook playbooks/llm-endpoint-preflight.yml --ask-vault-pass
```

### Multiple Models

Deploy multiple scenarios on the same cluster by running them sequentially.
Each scenario has its own GPU pool and namespace — no interference:

```bash
# Deploy two models side by side
ansible-playbook playbooks/llm-endpoint.yml -e @scenarios/llama-70b-benchmark.yml --ask-vault-pass
ansible-playbook playbooks/llm-endpoint.yml -e @scenarios/deepseek-r1-70b-benchmark.yml --ask-vault-pass

# Tear down one, keep the other
ansible-playbook playbooks/llm-endpoint-teardown.yml -e @scenarios/llama-70b-benchmark.yml --ask-vault-pass
```

> **Important:** Always run scenarios sequentially, not in parallel.
> Parallel runs can race on shared infrastructure and destabilize the cluster.

### Individual Steps

Run any layer independently:

```bash
ansible-playbook playbooks/rosa-cluster.yml --ask-vault-pass
ansible-playbook playbooks/step-cluster-access.yml --ask-vault-pass
ansible-playbook playbooks/rosa-gpu-pool.yml --ask-vault-pass -e gpu_instance_type=g6e.2xlarge
ansible-playbook playbooks/step-operators.yml --ask-vault-pass
ansible-playbook playbooks/step-gpu-stack.yml --ask-vault-pass
ansible-playbook playbooks/step-ai-platform.yml --ask-vault-pass
ansible-playbook playbooks/step-pull-secret.yml --ask-vault-pass
ansible-playbook playbooks/step-model.yml --ask-vault-pass
ansible-playbook playbooks/rosa-upgrade.yml --ask-vault-pass
```

## Getting Started

### Prerequisites

- Python 3.10+, Ansible 2.15+
- CLI tools: `aws`, `rosa`, `ocm`, `oc` (see `versions.yml` for minimum versions)
- AWS session (via `rh-aws-saml-login` or `aws sso login`)
- [OCM API token](https://console.redhat.com/openshift/token)
- [Red Hat registry service account](https://access.redhat.com/terms-based-registry/)

### Setup

```bash
# 1. Clone and install dependencies
git clone <repo-url> && cd openshift-playbook
ansible-galaxy collection install -r requirements.yml

# 2. Create your vault (you'll be prompted for a vault password)
ansible-vault create inventory/group_vars/all/vault.yml
```

Paste into the vault editor:

```yaml
---
vault_ocm_token: <your-ocm-token>
vault_registry_username: <your-rh-service-account>
vault_registry_password: <your-rh-service-account-password>
```

Optionally, save your vault password so you don't type it every run:

```bash
echo 'your-vault-password' > .vault_pass && chmod 600 .vault_pass
```

### Bootstrap

The bootstrap playbook auto-discovers your OIDC config, IAM roles, and VPC subnets,
then writes `inventory/group_vars/all/site.yml`:

```bash
rh-aws-saml-login                                          # authenticate AWS first
ansible-playbook playbooks/bootstrap.yml --ask-vault-pass  # discovers and writes site.yml
```

Bootstrap is idempotent — re-running only discovers values not yet in `site.yml`.

<details>
<summary>Manual setup (alternative to bootstrap)</summary>

```bash
cp inventory/group_vars/all/site.yml.example inventory/group_vars/all/site.yml
```

Then fill in each value. Discovery commands:

```bash
rosa list oidc-config                                       # OIDC config
rosa list account-roles | grep -i hcp                       # IAM roles
aws ec2 describe-subnets --region us-east-1 \               # Subnets
  --query 'Subnets[*].[SubnetId, AvailabilityZone,
    Tags[?Key==`Name`].Value | [0], MapPublicIpOnLaunch]' \
  --output table
```

</details>

### Deploy

```bash
ansible-playbook playbooks/llm-endpoint.yml --ask-vault-pass
```

The playbook will:
1. Log in to OCM/ROSA/oc
2. Create the ROSA HCP cluster (or skip if it exists)
3. Grant OCM access roles
4. Create a GPU machine pool and wait for the node
5. Install operators (NFD, GPU, Serverless, ServiceMesh, RHOAI)
6. Deploy the model and wait for the inference endpoint

Every step checks preconditions, skips if already done, and verifies postconditions.
If something fails, the error tells you exactly what to fix.

## Overlays

Three overlay directories customize deployments without changing code:

| Directory | What it overrides | Example |
|-----------|-------------------|---------|
| `scenarios/` | Model, GPU type, replica count, vLLM tuning | `-e @scenarios/mistral-24b-benchmark.yml` |
| `clusters/` | Cluster name, subnets, IAM, region | `-e @clusters/staging.yml` |
| `profiles/` | OCM access grants (users + roles) | `-e @profiles/my-team.yml` |

Overlays compose:

```bash
ansible-playbook playbooks/llm-endpoint-on-cluster.yml \
  -e @clusters/staging.yml \
  -e @scenarios/mistral-24b-benchmark.yml \
  -e @profiles/my-team.yml \
  --ask-vault-pass
```

<details>
<summary>Access profiles</summary>

Grant OCM roles to multiple users:

```yaml
# profiles/my-team.yml
cluster_access_grants:
  - username: alice
    account_id: abc123
    subscription_roles:
      - cluster_editor
      - machine_pool_editor
    groups:
      - cluster_admins

  - username: bob
    account_id: def456
    subscription_roles:
      - machine_pool_editor
    groups: []
```

```bash
ansible-playbook playbooks/step-cluster-access.yml -e @profiles/my-team.yml
```

Grant specs use key names (`cluster_editor`), not OCM IDs (`ClusterEditor`).
The mapping lives in `versions.yml`.

</details>

## Configuration

| File | Committed | Purpose |
|------|-----------|---------|
| `versions.yml` | yes | Version pins — operators, cluster, instance types, OCM API schema |
| `inventory/group_vars/all/config.yml` | yes | Public defaults — timeouts, GPU pool, model serving |
| `inventory/group_vars/all/site.yml` | **no** | Site-specific — cluster name, subnets, IAM ARNs |
| `inventory/group_vars/all/vault.yml` | **no** | Encrypted secrets — OCM token, registry credentials |
| `scenarios/*.yml` | yes | Model/GPU/tuning overrides |
| `clusters/*.yml` | **no** | Cluster-specific overrides |
| `profiles/*.yml` | **no** | Access grant profiles |

Templates for uncommitted files: `site.yml.example`, `clusters/example.yml`, `profiles/example.yml`.

## What the Playbook Manages

```
model
├── gpu_stack
│   ├── operators          # namespaces, subscriptions, wait for CRDs
│   └── gpu_machinepool    # create pool, wait for node
│   └── NFD instance → ClusterPolicy → wait for nvidia.com/gpu
├── ai_platform
│   └── operators          # (already resolved, skipped)
│   └── DSCI → DataScienceCluster → KServe → mesh membership
└── pull_secret            # registry.redhat.io credentials
└── ServingRuntime → InferenceService → wait for Ready
```

| Role | Purpose |
|------|---------|
| `bootstrap` | Auto-discover infrastructure, write `site.yml` |
| `login` | Authenticate AWS, ROSA, OCM sessions |
| `preflight` | Validate all constraints before changes |
| `rosa_cluster` | Create/wait for ROSA HCP cluster |
| `cluster_access` | Grant OCM roles (supports multi-user profiles) |
| `gpu_machinepool` | GPU machine pool with taint, wait for node |
| `operators` | Install NFD, GPU, Serverless, ServiceMesh, RHOAI |
| `gpu_stack` | NFD instance + ClusterPolicy, wait for GPU allocatable |
| `ai_platform` | DSCI + DataScienceCluster + KServe + mesh |
| `pull_secret` | Registry credentials in target namespace |
| `model` | ServingRuntime + InferenceService, wait for Ready |
| `llm_d` | Disaggregated prefill/decode via Helm (llm-d) |

## Design

- **Idempotent** — safe to re-run; preconditions skip what's already done
- **Fail fast** — validates AZ availability, vCPU quotas, and operator CRDs before making changes
- **Actionable errors** — every failure message tells you exactly what to fix and how
- **Version-pinned** — `versions.yml` locks operator channels, instance types, cluster version, and OCM API schema
- **Composable** — run the full pipeline or any step independently

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for role conventions, schema-as-data patterns,
and extension guides.
