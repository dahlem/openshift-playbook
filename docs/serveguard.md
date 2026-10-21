# ServeGuard demo on ROSA

This page takes you from an empty AWS account to the ServeGuard OpenShift AI demo
on ROSA HCP, with one playbook run. The demo shows a proof-carrying LoRA adapter
being admitted by a receiver Job, gated by a Kubernetes `ValidatingAdmissionPolicy`,
and served by vLLM under KServe standard (RawDeployment) mode. Four tampering
attempts are refused.

The demo is defined in the serveguard repository
(`integrations/openshift-ai`; chapter `docs/playbook/serveguard/openshift-ai.md`
there). This repository provides the platform under it and runs the demo's
Makefile:

| Layer | Owner | What |
|-------|-------|------|
| ROSA HCP cluster, serving machine pool | this repo (`rosa_cluster`, `gpu_machinepool`) | 2x `m5.2xlarge` workers + 1 serving node |
| Operators | this repo (`operators`) | cert-manager, RHOAI (`rhods-operator`), and NFD + NVIDIA GPU for `gpu.yml` |
| DataScienceCluster | this repo (`ai_platform`, `ai_platform_mode: standard`) | DSC v2, `kserve` Managed (NIM Removed), every other component Removed |
| Demo | serveguard (`make demo-up`, `make demo-stories`) | namespace, receiver build into the internal registry, publish, admit, serve, stories |
| Admission policy | serveguard (applied by `demo-up`) | `ValidatingAdmissionPolicy serveguard-admitted-adapters-<namespace>` |

The playbook never calls the integration's `make env-up`. Do not run `env-up` on a
cluster this playbook installed: it would add a second `OperatorGroup` in
`redhat-ods-operator` and `cert-manager-operator`, and OLM refuses a namespace with
two `OperatorGroup`s.

> **Status: validated offline only.** The playbooks pass `ansible-playbook
> --syntax-check`, and the operator resolution and story summary were tested
> offline. Nothing here has run against a cluster yet. The output shown below is
> the *expected* form. See [What to confirm on the first run](#what-to-confirm-on-the-first-run).

## Scenarios

| Scenario | Serving | Serving pool | Operators |
|----------|---------|--------------|-----------|
| `scenarios/serveguard/cpu.yml` | `registry.redhat.io/rhaii/vllm-cpu-rhel9:3.5.0` (x86_64 CPU, custom runtime) | 1x `m6i.2xlarge` (8 vCPU, 32 GiB) | cert-manager, RHOAI |
| `scenarios/serveguard/gpu.yml` | `registry.redhat.io/rhaii/vllm-cuda-rhel9:3.5.0`, 1x `nvidia.com/gpu` | 1x `g6.2xlarge` (1x L4, 8 vCPU, 32 GiB) | cert-manager, NFD, NVIDIA GPU, RHOAI |

Both serve `HuggingFaceTB/SmolLM2-135M-Instruct` at a pinned revision. Pins live in
`versions.yml` under `serveguard:`.

Sizing. The predictor requests and limits 4 vCPU and 8 GiB, the integration's
default (the Makefile does not expose it). A 4 vCPU node cannot hold that next to
the node's own daemons. So the serving pool is 8 vCPU. For GPU that means
`g6.2xlarge`, not `g6.xlarge`. The two default `m5.2xlarge` workers carry the
operators, KServe, the receiver build and the Jobs. That matches the RHOAI 3.5
multi-node minimum of 2 workers with 8 vCPU and 32 GiB each.

The serving pool is untainted, because the rendered predictor has no
`nvidia.com/gpu` toleration. Other pods can therefore land on the GPU node too.

## Cost and time

These are estimates from us-east-1 on-demand list prices (October 2026). ROSA HCP
costs $0.25/h per cluster plus $0.171/h per 4 worker vCPU. EC2 prices are AWS list
prices. Check both against the AWS pricing pages for your region.

| Item | CPU scenario | GPU scenario |
|------|-------------:|-------------:|
| ROSA HCP cluster fee | $0.25 | $0.25 |
| 2x `m5.2xlarge` workers: EC2 ($0.384 each) + ROSA fee ($0.342 each) | $1.45 | $1.45 |
| Serving node: EC2 | `m6i.2xlarge` $0.38 | `g6.2xlarge` $0.98 |
| Serving node: ROSA fee (8 vCPU) | $0.34 | $0.34 |
| **Total, per hour** | **about $2.45** | **about $3.00** |

On top of that come EBS root volumes (about $0.03/h per node), the NAT gateway
and load balancers of the VPC (about $0.05 to $0.10/h), and data transfer. Budget
**about $2.60/h (CPU)** or **$3.20/h (GPU)** while the cluster exists. A full run
plus a teardown costs roughly $3 to $5.

Wall-clock time from an empty account, expected:

| Step | Time |
|------|------|
| One-time account setup (roles, VPC, OIDC) | 10 min |
| ROSA HCP cluster ready | 15 to 30 min |
| Serving pool node Ready | 5 to 10 min |
| cert-manager + RHOAI + DSC Ready | 10 to 15 min |
| GPU stack (NFD, driver build) - `gpu.yml` only | 10 to 20 min |
| `make demo-up` (receiver image build, fetch, publish, admit, vLLM image pull) | 20 to 30 min |
| `make demo-stories` | 10 to 15 min |
| **Total** | **about 75 to 110 min** |

## Prerequisites

Workstation:

| Tool | Version | Why |
|------|---------|-----|
| Python | 3.10+ | Ansible |
| `ansible-core` | 2.15+ (checked with 2.20) | playbooks |
| Python packages `kubernetes`, `jmespath` | current | `kubernetes.core` modules and `json_query` |
| `aws` CLI | v2 | account setup, preconditions |
| `rosa` CLI | `>=1.2.60` (`versions.yml`) | cluster and machine pools |
| `ocm` CLI | current | OCM login and access grants |
| `oc` | 4.19+ | the demo targets OpenShift 4.19+ (`versions.yml` requires 4.17+ for the other scenarios) |
| `make`, `uv`, `curl`, `python3` | current | the serveguard Makefile |

No container engine is needed: the receiver image is built in the cluster.

Accounts and credentials:

- An AWS account with ROSA enabled and linked to your Red Hat account (AWS
  console, *Red Hat OpenShift Service on AWS*, *Get started*).
- An [OCM API token](https://console.redhat.com/openshift/token).
- EC2 quota, region `us-east-1` by default:
  - *Running On-Demand Standard (A, C, D, H, I, M, R, T, Z) instances*: at least
    24 vCPU (2 workers + the CPU serving node). For `gpu.yml`, 16.
  - `gpu.yml` only: *Running On-Demand G and VT instances*: at least 8 vCPU. New
    accounts often start at 0. The `gpu_machinepool` preconditions check this and
    print the quota request command.
- The serveguard repository checked out **next to** this one (`../serveguard`).
  Or pass `-e serveguard_repo_dir=/path/to/serveguard`.

The vLLM images come from `registry.redhat.io`. The ROSA cluster's global pull
secret already covers it, so the vault needs only the OCM token.

## Reproducible sequence

### 1. Workstation

```bash
git clone <openshift-playbook-url> openshift-playbook
git clone <serveguard-url> serveguard          # sibling of openshift-playbook
cd openshift-playbook
python3 -m pip install 'ansible-core>=2.15' kubernetes jmespath
ansible-galaxy collection install -r requirements.yml
```

### 2. Vault

```bash
ansible-vault create inventory/group_vars/all/vault.yml
```

```yaml
---
vault_ocm_token: <your-ocm-token>
```

Optionally, store the vault password in `.vault_pass` (gitignored, `chmod 600`).
Then you can drop `--ask-vault-pass` from the commands below.

### 3. One-time AWS account setup

These commands create AWS resources. Run each one once per account.

```bash
aws configure                          # or: rh-aws-saml-login / aws sso login
rosa login --token=<your-ocm-token>
rosa verify quota --region us-east-1
aws iam create-service-linked-role --aws-service-name elasticloadbalancing.amazonaws.com \
  || true                              # already exists in most accounts
rosa create account-roles --mode auto --hosted-cp --yes
rosa create network                    # VPC with public + private subnets (CloudFormation); see rosa create network --help
```

`rosa create network` exists only in recent `rosa` releases. Check
`rosa create network --help`. You can use any VPC that has
one public and one private subnet in the same AZ instead. The OIDC config is
created by the bootstrap step if none exists.

### 4. Bootstrap `site.yml`

```bash
ansible-playbook playbooks/bootstrap.yml --ask-vault-pass -e rosa_cluster_name=serveguard
```

This finds the OIDC config, the HCP account roles and a subnet pair, then writes
`inventory/group_vars/all/site.yml`. It prompts when there is more than one
candidate.

### 5. Deploy

```bash
# CPU (default)
ansible-playbook playbooks/serveguard-demo.yml -e @scenarios/serveguard/cpu.yml --ask-vault-pass

# or GPU (1x L4)
ansible-playbook playbooks/serveguard-demo.yml -e @scenarios/serveguard/gpu.yml --ask-vault-pass
```

The playbook runs these roles in order:

1. `login`: AWS, OCM, ROSA, then `oc` as `cluster-admin`. On the first run it
   creates the admin, once the cluster exists.
2. `rosa_cluster`: creates the HCP cluster (OpenShift `versions.cluster.openshift_version`)
   and waits for it to be `ready`.
3. `cluster_access`: OCM role grants (`profiles/`).
4. `gpu_machinepool`, run with the serving pool's variables: `sg-cpu` (`m6i.2xlarge`)
   or `sg-gpu` (`g6.2xlarge`), 1 node, no taint. It waits for the node to be Ready.
5. `operators` with `operators_enabled: [cert_manager, rhoai]`
   (`gpu.yml`: `[cert_manager, nfd, gpu, rhoai]`). Before subscribing, it checks
   that the requested channels exist in the catalog.
6. `gpu_stack` (`gpu.yml` only): NFD instance, ClusterPolicy, waits for allocatable `nvidia.com/gpu`.
7. `ai_platform` with `ai_platform_mode: standard`: waits for the cert-manager
   webhook and the RHOAI CSV. It waits for the operator's `default-dsci`, and
   creates one only if the operator has not. It applies the DSC v2 and waits for
   `kserve-controller-manager` and for the DSC to be `Ready`.
8. `serveguard_demo`: checks the checkout, `make`/`uv`, KServe CRDs, the internal
   registry (`Managed`) and a default StorageClass. Then it runs `make demo-up`
   with `NAMESPACE`, `ISVC`, `ACCELERATOR`, `VLLM_IMAGE`, `MODEL`,
   `MODEL_REVISION`, `RHOAI_CHANNEL` and `TIMEOUT`. It asserts that the
   `InferenceService` is Ready with `serveguard.io/admitted: "true"` and that the
   policy exists.
9. `serveguard_demo` stories: runs `make demo-stories`, saves the full output
   under `logs/`, and prints the summary.

Every step is idempotent. A re-run skips the cluster, pool, operators and DSC if
they already hold. It also skips `demo-up` if the admitted service is Ready. Pass
`-e serveguard_redeploy=true` to publish and admit again. Pass
`-e serveguard_run_stories=false` to stop before the stories.

If you already have a ROSA HCP cluster, use the same command with
`playbooks/serveguard-demo-on-cluster.yml`.

### 6. Expected output

Not yet observed on a cluster. The form comes from the roles' banners.

```
============================================================
  ServeGuard adapter admitted and serving
============================================================

  InferenceService: serveguard-demo/serveguard-demo (KServe standard)
  Model:            HuggingFaceTB/SmolLM2-135M-Instruct@12fd25f77366
  Serving:          cpu (registry.redhat.io/rhaii/vllm-cpu-rhel9:3.5.0)
  RHOAI channel:    stable-3.5
  Policy:           ValidatingAdmissionPolicy serveguard-admitted-adapters-serveguard-demo
  Verdict:
    serveguard.io/adapter-digest = 28266fff...
    serveguard.io/admitted = true
    serveguard.io/base-hash = ecf2f3e1...
    serveguard.io/package-root = 17a154dc...

============================================================

============================================================
  ServeGuard on OpenShift AI: all stories as expected
============================================================

  served       admitted adapter answers: "..."
  refused      an InferenceService without a ServeGuard verdict
               denied: adapter must be admitted by ServeGuard (...)
  refused      a verdict written by anyone but the receiver
               denied: only the ServeGuard receiver (system:serviceaccount:serveguard-demo:serveguard-receiver) ...
  refused      changing the admitted service's adapter digest
               denied: only the ServeGuard receiver ...
  refused      an adapter whose read factor sees past the monitor
               reason: layer 0: served (A, C, rho) do not open the commitment
  refused      a recommitted package (certificate) under a new root
               reason: package root does not match trusted identity
  refused      adapter bytes changed after admission; the predictor never restarts serving
               serveguard: REFUSED: adapter bytes differ from the admitted adapter digest
  restored     restore the admitted adapter bytes; the guard admits them again
               inferenceservice.serving.kserve.io/serveguard-demo condition met

  Full output: logs/serveguard-demo-stories-<timestamp>.log

============================================================
```

The summary only reports. The pass/fail signal is the Makefile's exit code: an
apply that is accepted, or a Job that does not fail, stops `make` with a non-zero
code. The playbook then prints `FAILED (make rc=N)` and the last lines, and fails.
For what each story proves, read the serveguard chapter.

### 7. Teardown

```bash
# Demo + platform + serving pool (keeps the cluster)
ansible-playbook playbooks/serveguard-demo-teardown.yml -e @scenarios/serveguard/cpu.yml --ask-vault-pass

# Demo only (keeps RHOAI, cert-manager and the serving pool)
ansible-playbook playbooks/serveguard-demo-teardown.yml -e @scenarios/serveguard/cpu.yml -e teardown_platform=false --ask-vault-pass

# Everything, including the ROSA cluster (stops all charges except the VPC's NAT gateway)
ansible-playbook playbooks/serveguard-demo-teardown.yml -e @scenarios/serveguard/cpu.yml -e teardown_cluster=true --ask-vault-pass
```

Pass the scenario you deployed: it selects which operators and which pool to
remove. The teardown runs `make demo-down` (namespace, then the cluster-scoped
policy and binding). It then deletes the DSC and DSCI, the operator subscriptions
and CSVs, and the serving pool. CRDs are left in place. To remove the one-time
account resources afterwards, delete the network stack (`rosa create network`
printed its CloudFormation stack name), the OIDC config (`rosa delete oidc-config`)
and the account roles (`rosa delete account-roles --mode auto`).

## Switching to RHOAI 3.6

RHOAI 3.6 is not GA as of 2026-10-21. The channel is one variable:

```bash
ansible-playbook playbooks/serveguard-demo.yml -e @scenarios/serveguard/cpu.yml \
  -e rhoai_channel=stable-3.6 --ask-vault-pass
```

Put `-e rhoai_channel=...` after the scenario file, so it wins. To make 3.6 the
default, set `serveguard.rhoai_channel: stable-3.6` in `versions.yml`.

- The `operators` role first checks the channel against the cluster's
  `rhods-operator` PackageManifest and fails with the list of available channels.
  You can also list them yourself: `make -C ../serveguard/integrations/openshift-ai channels`.
- On a cluster that runs 3.5, the role sees that the Subscription's channel
  differs and re-applies it. OLM then upgrades in place (install plan approval
  `Automatic`).
- Use the CPU/CUDA image that the Red Hat AI supported-configurations page lists
  for 3.6: `-e serveguard_vllm_image=registry.redhat.io/rhaii/vllm-cpu-rhel9:<3.6 tag>`
  (or update `versions.yml`).
- If the 3.6 API rejects or drops a DSC component, override the list. For
  example: `-e '{"ai_platform_standard_removed": ["aipipelines", "dashboard", ...]}'`.
  The default list is in `roles/ai_platform/defaults/main.yml`.
- Check that the pinned OpenShift version is supported by 3.6.

## Variables

| Variable | Default (cpu.yml) | Meaning |
|----------|-------------------|---------|
| `rhoai_channel` | `versions.serveguard.rhoai_channel` (`stable-3.5`) | RHOAI Subscription channel |
| `operators_enabled` | `[cert_manager, rhoai]` | operators to install, in order |
| `operators_validate_channels` | `true` | fail before subscribing if a channel is not in the catalog |
| `ai_platform_mode` | `standard` | DSC v2, KServe standard only |
| `ai_platform_standard_removed` | 16 components | DSC components set `Removed` |
| `serveguard_accelerator` | `cpu` (`nvidia` in gpu.yml) | Makefile `ACCELERATOR` |
| `serveguard_pool_name` / `serveguard_pool_instance_type` | `sg-cpu` / `m6i.2xlarge` | serving machine pool |
| `serveguard_vllm_image` | `versions.serveguard.vllm_cpu_image` | Makefile `VLLM_IMAGE` |
| `serveguard_namespace` | `serveguard-demo` | Makefile `NAMESPACE` |
| `serveguard_image` | `""` (in-cluster build) | Makefile `IMAGE` |
| `serveguard_storage_class` | `""` (cluster default) | Makefile `STORAGE_CLASS` |
| `serveguard_repo_dir` | `../serveguard` | serveguard checkout |
| `serveguard_redeploy` | `false` | re-run `demo-up` even if admitted and Ready |
| `serveguard_run_stories` | `true` | run `demo-stories` |

## What to confirm on the first run

These were not observable offline:

- **Channel names.** `stable-3.5` follows the `stable-x.y` pattern from the RHOAI
  3.5 install guide. The guide gives no 3.x example. The playbook checks the name
  against the catalog before subscribing.
- **OpenShift version.** `versions.yml` pins 4.21.3. The serveguard chapter lists
  RHOAI 3.5 on 4.19.9 to 4.22 (Supported Configurations article). The 3.5 install
  guide text says 4.19 to 4.20. Before the first run, check
  `rosa list versions --hosted-cp` and the supported-configurations article. To
  pin a different version for this demo only, use a separate `versions_file`.
- **`default-dsci`.** The playbook assumes the operator creates it (DSCI v2) and
  creates one only if it is missing after 3 minutes.
- **DSC v2 component names.** They are taken from the opendatahub-operator
  v3.5.0 source via the serveguard integration, not from a live 3.5 CRD.
- **Deployment names.** `kserve-controller-manager` in `redhat-ods-applications`
  (from the 3.5 install guide) and `cert-manager-webhook` in `cert-manager`.
- **Scheduling.** Whether the 4 vCPU / 8 GiB predictor and the receiver build fit
  as sized. Whether `--dtype=float32` with LoRA works on the CPU image (an open
  point in the serveguard chapter).
- **GPU path.** It is untested end to end. The CUDA image's server command
  (`python -m vllm.entrypoints.openai.api_server`, the Makefile default) is
  assumed.
