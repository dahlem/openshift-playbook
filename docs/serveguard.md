# ServeGuard demo on ROSA

This page takes you from an empty AWS account to the ServeGuard OpenShift AI demo
on ROSA HCP, with one playbook run. The demo shows a proof-carrying LoRA adapter
being admitted by a receiver Job, gated by a Kubernetes `ValidatingAdmissionPolicy`,
and served by vLLM under KServe standard (RawDeployment) mode. Four tampering
attempts are refused.

The demo is defined in the serveguard repository
(`integrations/openshift-ai`; chapter `docs/playbook/serveguard/openshift-ai.md`
there). It ships as the Ansible collection `serveguard.openshift_ai`
(`integrations/openshift-ai/ansible/`). This repository provides the platform
under it and runs the collection's roles:

| Layer | Owner | What |
|-------|-------|------|
| ROSA HCP cluster, serving machine pool | this repo (`rosa_cluster`, `gpu_machinepool`) | 2x `m5.2xlarge` workers + 1 serving node |
| Operators | this repo (`operators`) | cert-manager, RHOAI (`rhods-operator`), and NFD + NVIDIA GPU for `gpu.yml` |
| DataScienceCluster | this repo (`ai_platform`, `ai_platform_mode: standard`) | DSC v2, `kserve` Managed (NIM Removed), every other component Removed |
| Registry, StorageClass, cluster capacity checks | this repo (`serveguard_checks`) | read-only; the workload's requests come from the collection's role defaults |
| Workload | `serveguard.openshift_ai` roles `preflight` -> `receiver_image` -> `publish` -> `admit` -> `serve` -> `stories`; `teardown` | namespace, receiver build into the internal registry, publish, admit, serve, stories, each with its own pre/postconditions |
| Admission policy | `serveguard.openshift_ai` | `ValidatingAdmissionPolicy serveguard-admitted-adapters-<namespace>` |

The playbooks never run the integration's environment setup (`make env-up`). Do not
run `env-up` on a cluster this playbook installed: it would add a second `OperatorGroup` in
`redhat-ods-operator` and `cert-manager-operator`, and OLM refuses a namespace with
two `OperatorGroup`s.

> **Status: validated offline only.** All playbooks pass `ansible-playbook
> --syntax-check`. `tests/serveguard/run.sh` runs the check decisions on fixture
> JSON, the preflight sequence over whole-account fixtures, the Mistral identity
> check, and the workload wiring against a **stub** `serveguard.openshift_ai`
> collection (the real one is not published yet). Nothing here has run against a
> cluster or an AWS account yet, so the output shown below is the *expected* form. See
> [What to confirm on the first run](#what-to-confirm-on-the-first-run).

## Scenarios

| Scenario | Serving | Serving pool | Operators |
|----------|---------|--------------|-----------|
| `scenarios/serveguard/cpu.yml` | `registry.redhat.io/rhaii/vllm-cpu-rhel9:3.5.0` (x86_64 CPU, custom runtime) | 1x `m6i.2xlarge` (8 vCPU, 32 GiB) | cert-manager, RHOAI |
| `scenarios/serveguard/gpu.yml` | `registry.redhat.io/rhaii/vllm-cuda-rhel9:3.5.0`, 1x `nvidia.com/gpu` | 1x `g6.2xlarge` (1x L4, 8 vCPU, 32 GiB) | cert-manager, NFD, NVIDIA GPU, RHOAI |

The model, its revision and every workload resource request are the collection's
role defaults. This repository pins only the platform side (`versions.yml`,
`serveguard:`): RHOAI channel, the vLLM images per scenario and the pool types.

Sizing. The predictor requests and limits `serveguard_predictor_cpu` /
`serveguard_predictor_memory`, which default to 4 vCPU and 8 GiB in the
collection. The quota and capacity checks read these values from the collection;
nothing in this repository restates them. Override both in a scenario or with `-e`:
the checks and the collection then use the override. A 4 vCPU node cannot hold that next to
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
| workload: receiver image build, publish, admit, serve (vLLM image pull) | 20 to 30 min |
| workload: stories | 10 to 15 min |
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
| `serveguard.openshift_ai` collection | from `requirements.yml` (git) or `requirements-dev.yml` (sibling checkout) | the workload; its `preflight` role checks its own tools |

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
- For local development, the serveguard repository checked out **next to** this
  one (`../serveguard`). It is installed from there with `requirements-dev.yml`.

The vLLM images come from `registry.redhat.io`. The ROSA cluster's global pull
secret already covers it, so the vault needs only the OCM token.

## Reproducible sequence

### 1. Workstation

```bash
git clone <openshift-playbook-url> openshift-playbook
cd openshift-playbook
python3 -m pip install 'ansible-core>=2.15' kubernetes jmespath
ansible-galaxy collection install -r requirements.yml       # includes serveguard.openshift_ai from git
```

`requirements.yml` installs `serveguard.openshift_ai` from
`https://github.com/dahlem/serveguard.git#/integrations/openshift-ai/ansible`
(branch `main`). To work against a local checkout instead:

```bash
git clone https://github.com/dahlem/serveguard.git ../serveguard   # sibling of openshift-playbook
ansible-galaxy collection install -r requirements-dev.yml --force   # type: dir; re-run after changing it
```

Both install into `./collections` (`ansible.cfg`). The preflight's `workload` rows
show which version is in use and from where.

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

### 5. Preflight (read-only)

```bash
ansible-playbook playbooks/serveguard-demo-preflight.yml -e @scenarios/serveguard/cpu.yml --ask-vault-pass
```

This runs every platform precondition below against the account and, if it
exists, the cluster. It then runs the collection's own read-only
`serveguard.openshift_ai.preflight` role, once the cluster is reachable. It prints
one table and changes nothing: no cluster, pool, admin user,
operator or namespace is created. Cluster-side rows show `N/A` until the cluster
exists and `oc` is logged in to it. The playbook exits non-zero if any row is
`FAIL` or `CONFLICT`. Fix those rows, re-run until it says `Ready to deploy`, then
deploy. See [Safety](#safety-preconditions-postconditions-and-skips) for what
each status means.

### 6. Deploy

```bash
# CPU (default)
ansible-playbook playbooks/serveguard-demo.yml -e @scenarios/serveguard/cpu.yml --ask-vault-pass

# or GPU (1x L4)
ansible-playbook playbooks/serveguard-demo.yml -e @scenarios/serveguard/gpu.yml --ask-vault-pass
```

Platform steps run as *pre → step → post* (`playbooks/includes/serveguard-demo-steps.yml`):

1. `login`, then the read-only checks: local tools, the workload requests
   (`serveguard.openshift_ai` role defaults), the AWS account, cluster, serving
   pool, and quotas/instance types.
2. `rosa_cluster` creates the HCP cluster (OpenShift `versions.cluster.openshift_version`),
   only if it is absent or still installing. `login` then logs `oc` in.
3. `cluster_access`: OCM role grants (`profiles/`).
4. `gpu_machinepool` runs with the serving pool's variables: `sg-cpu`
   (`m6i.2xlarge`) or `sg-gpu` (`g6.2xlarge`), 1 node, no taint. It runs only if
   the pool is absent or has no Ready node yet.
5. `operators` runs only for the operators that are not yet `Succeeded` on the
   requested channel: `[cert_manager, rhoai]`, plus `nfd` and `gpu` for `gpu.yml`.
6. `gpu_stack` (`gpu.yml` only): NFD instance and ClusterPolicy, unless
   `nvidia.com/gpu` is already allocatable.
7. `ai_platform` with `ai_platform_mode: standard`. It waits for the cert-manager
   webhook and the RHOAI CSV, then for the operator's `default-dsci`, and creates
   one only if the operator has not. It applies the DSC v2 unless it is already
   `Ready` with the required component states.
8. The registry, StorageClass and capacity checks for the workload.
9. The workload (`playbooks/includes/serveguard-demo-workload.yml`): the
   collection roles `preflight` → `receiver_image` → `publish` → `admit` →
   `serve` → `stories`. Each role checks its own pre- and postconditions, skips
   what already holds and stops on conflicts. Variables a scenario or `-e` sets
   (`serveguard_predictor_cpu`, `serveguard_vllm_image`, ...) reach the roles;
   everything else is the collection's default.
10. The story banner and the table of every platform check.

Pass `-e serveguard_run_stories=false` to stop before the stories. The
collection's own variables (for example re-admitting) are documented in its
README. If you already have a ROSA HCP cluster, use the same command with
`playbooks/serveguard-demo-on-cluster.yml`. It never creates a cluster: a missing
one is a `FAIL`.

### 7. Expected output

Not yet observed on a cluster. The banner is built from the `stories` role's
structured result, `serveguard_stories_result`, a list of
`{story, expected, verdict, reason}`. The story names and reasons below are
illustrative; the collection defines them.

```
============================================================
  ServeGuard on OpenShift AI: all stories as expected
============================================================

  served       admitted adapter serves
               completion: ...
  refused      unadmitted InferenceService
               denied: adapter must be admitted by ServeGuard
  refused      forged verdict
               denied: only the ServeGuard receiver may create ...
  refused      tampered read factor
               layer 0: served (A, C, rho) do not open the commitment
  refused      recommitted package
               package root does not match trusted identity
  refused      bytes changed after admission
               adapter bytes differ from the admitted adapter digest
  restored     restore

============================================================
```

A story passes when its verdict equals its expected verdict. Any other story is
marked `(expected ...)`, the header says how many were not as expected, and the
playbook fails after printing the banner. If the `stories` role fails itself, the
banner still prints whatever it recorded. For what each story proves, read the
serveguard chapter.

### 8. Teardown

```bash
# Workload + platform + serving pool (keeps the cluster)
ansible-playbook playbooks/serveguard-demo-teardown.yml -e @scenarios/serveguard/cpu.yml --ask-vault-pass

# Workload only (keeps RHOAI, cert-manager and the serving pool)
ansible-playbook playbooks/serveguard-demo-teardown.yml -e @scenarios/serveguard/cpu.yml -e teardown_platform=false --ask-vault-pass

# Everything, including the ROSA cluster (stops all charges except the VPC's NAT gateway)
ansible-playbook playbooks/serveguard-demo-teardown.yml -e @scenarios/serveguard/cpu.yml -e teardown_cluster=true --ask-vault-pass
```

Pass the scenario you deployed: it selects which operators and which pool to
remove. The teardown runs `serveguard.openshift_ai.teardown` (the workload,
including the cluster-scoped policy and binding). It then deletes the DSC and DSCI, the operator subscriptions
and CSVs, and the serving pool. CRDs are left in place. To remove the one-time
account resources afterwards, delete the network stack (`rosa create network`
printed its CloudFormation stack name), the OIDC config (`rosa delete oidc-config`)
and the account roles (`rosa delete account-roles --mode auto`).

## Safety: preconditions, postconditions and skips

Every platform step is guarded by `roles/serveguard_checks`. The workload steps
are guarded by the `serveguard.openshift_ai` roles themselves. Before a platform
step, the checks read the
current state, read-only (`rosa describe`, `aws ... describe/get`, `oc get`). It
then decides one of:

| Status | Meaning | Effect |
|--------|---------|--------|
| `OK` | a precondition holds | continue |
| `SATISFIED` | the step's end state already holds | **the step is skipped** ("already satisfied") |
| `TODO` | missing | the step creates it |
| `WAIT` | exists and is converging | the step re-applies nothing new and waits |
| `OVERRIDE` | conflicting, accepted by an `serveguard_allow_*` variable | as documented per row |
| `N/A` | cannot be checked yet (an earlier step has not run) | checked again when the step is reached |
| `WARN` | worth reading | continue |
| `CONFLICT` | exists in a state the step would have to change or break | **stop before changing anything** |
| `FAIL` | a precondition does not hold | **stop before changing anything** |

After a step, its postcondition waits a bounded time, then re-reads the state and
requires `SATISFIED`. On timeout it prints read-only diagnostics (conditions,
events, CSV phase, logs) and fails. The step is safe to re-run once the cause is
fixed. The deploy stops at the first blocking row. The preflight reports all rows.

| Step | Precondition (fails or conflicts when ...) | Skipped when | Postcondition (bounded wait, diagnostics) |
|------|-------------------------------------------|--------------|---------------------------------------------|
| local | `rosa` >= 1.2.60, `aws` >= 2, `oc` >= 4.19, `ansible-core` >= 2.15, `ocm` missing/old; Python `kubernetes`/`jmespath` not importable (the workload's tools: its `preflight` role) | - | - |
| workload requests | `serveguard.openshift_ai` not installed; its role defaults lack `serveguard_predictor_cpu`/`_memory` (other requests missing: `WARN`, their capacity rows are left out) | - | - |
| AWS account (bootstrap output) | no AWS session or ROSA login; site.yml incomplete; OIDC config, installer/support/worker roles or ELB service-linked role missing; subnets missing or in different VPCs; < 8 free subnet tags | - | - |
| quotas | instance type not offered in the private subnet's AZ or not ROSA-supported; Standard (`L-1216C47A`) or G/VT (`L-DB2E81BA`) vCPU quota minus running vCPUs < vCPUs this run adds (2 workers if the cluster is new + the serving pool if new); serving type cannot hold predictor + one Job (vCPU/memory minus `serveguard_node_overhead`); `nvidia` on a non-GPU type | - | - |
| cluster | exists but ROSA classic, other region, state `error`/`uninstalling`, or OpenShift outside 4.19.9 <= v < 4.23 (`serveguard_allow_cluster_version`); on-cluster playbook: absent | `ready` at a supported version | `ready` (`cluster_wait_timeout`), oc logged in to its API; diag: `rosa describe`, `rosa logs install` |
| serving pool | exists with another instance type, replicas/autoscaling, taints, labels or subnet (`serveguard_allow_pool_mismatch` uses it as is) | exists as specified with a Ready node | a Ready node of the type (`node_wait_timeout`); diag: pool status, nodes, node events |
| operators (each) | package or channel not in the catalog; package subscribed elsewhere; 2+ OperatorGroups in the namespace; an OperatorGroup with another target mode; Subscription on another channel (`serveguard_allow_channel_change`) or another package/source; CSV `Failed` | Subscription on the requested channel and CSV `Succeeded` (a compatible foreign OperatorGroup is reused, never duplicated) | installedCSV set and `Succeeded` (`crd_wait_timeout`); diag: OperatorGroup/Subscription/InstallPlan/CSV, Subscription conditions, CSV reason, warning events |
| GPU stack (gpu.yml) | no Ready GPU node | `nvidia.com/gpu` allocatable and ClusterPolicy `ready` | `nvidia.com/gpu` allocatable (20 min); diag: ClusterPolicy status, non-running operator pods, events |
| DSCI / DSC | DSC CRD missing; 2+ DSCIs or a DSCI not named `default-dsci`; a DSC under another name; `default-dsc` with other component states, e.g. a Knative install with the dashboard Managed (`serveguard_allow_dsc_update`) | `default-dsc` `Ready`, kserve Managed, NIM and the 16 others Removed, KServe controller ready | DSC `Ready` (20 min); diag: DSC status, DSCI phase, RHOAI CSV, deployments, non-running pods, events, cert-manager pods |
| internal registry | not `Managed`, no storage, no ready `image-registry` (n/a when the collection's `serveguard_image` names an external image) | - | - |
| StorageClass | no default (or the named `serveguard_storage_class`) StorageClass; no dynamic provisioner; the collection's `serveguard_store_access_mode` is ReadWriteMany on EBS | - | - |
| capacity | no Ready, untainted, schedulable node has free requests (allocatable minus running pods, init containers counted) for each workload request read from the collection: receiver build, a Job, the predictor (`serveguard_predictor_*`, + 1 GPU for nvidia, `serveguard_min_node_ephemeral` 30Gi), and predictor + a co-located story Job. Reported `N/A` while the serving pool is still to be created | - | - |
| workload | the collection's roles (`preflight` ... `stories`) | the collection's roles | the collection's roles; then every story's verdict equals its expected verdict (banner) |

The decisions are in `roles/serveguard_checks/filter_plugins/serveguard_checks.py`,
as pure functions of what was read. `tests/serveguard/run.sh` runs them offline
on fixture JSON: 61 decision cases and 3 whole-account worlds through the
preflight sequence. It also runs the Mistral identity check and the workload
wiring against a stub collection (`tests/serveguard/fixtures/collections`): role
order, reading the requests from role defaults, override pass-through, the banner,
and failure on a story that is not as expected.

## Switching to RHOAI 3.6

RHOAI 3.6 is not GA as of 2026-10-21. The channel is one variable:

```bash
ansible-playbook playbooks/serveguard-demo.yml -e @scenarios/serveguard/cpu.yml \
  -e rhoai_channel=stable-3.6 --ask-vault-pass
```

Put `-e rhoai_channel=...` after the scenario file, so it wins. To make 3.6 the
default, set `serveguard.rhoai_channel: stable-3.6` in `versions.yml`.

- The preflight and the operators precondition check the channel against the
  cluster's `rhods-operator` PackageManifest. If it is missing, they fail with the
  list of available channels. You can also list them yourself:
  `make -C ../serveguard/integrations/openshift-ai channels`.
- A cluster that already runs 3.5 has its Subscription on `stable-3.5`. That is a
  `CONFLICT`: the playbook stops and changes nothing. To upgrade in place, add
  `-e serveguard_allow_channel_change=true`. The Subscription is then re-applied
  on the new channel, and OLM upgrades it (install plan approval `Automatic`).
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
| `ai_platform_mode` | `standard` | DSC v2, KServe standard only |
| `ai_platform_standard_removed` | 16 components | DSC components set `Removed` |
| `serveguard_accelerator` | `cpu` (`nvidia` in gpu.yml) | GPU checks and pool; passed to the collection |
| `serveguard_pool_name` / `serveguard_pool_instance_type` | `sg-cpu` / `m6i.2xlarge` | serving machine pool |
| `serveguard_vllm_image` | `versions.serveguard.vllm_cpu_image` (scenario) | passed to the collection |
| `serveguard_run_stories` | `true` | run the `stories` role |
| `serveguard_predictor_cpu` / `serveguard_predictor_memory` | collection default (`"4"` / `8Gi`) | set only to override; the capacity checks follow |
| `serveguard_namespace`, `serveguard_image`, `serveguard_storage_class`, `serveguard_store_access_mode`, `serveguard_*_resources` | collection defaults | read by the platform checks (`serveguard_workload_vars`); set only to override |
| `serveguard_allow_cluster_version` | `false` | accept a cluster outside 4.19.9 <= v < 4.23 |
| `serveguard_allow_pool_mismatch` | `false` | use an existing pool whose spec differs, as is |
| `serveguard_allow_channel_change` | `false` | move an existing Subscription to the requested channel |
| `serveguard_allow_dsc_update` | `false` | change component states of an existing DSC |

The platform-side `serveguard_*` settings live in
`inventory/group_vars/all/config.yml`. The workload's settings are the
`serveguard.openshift_ai` role defaults. Nothing in this repository restates them.

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
- **GPU path.** It is untested end to end. The CUDA image's server command is
  the collection's concern.
- **The `serveguard.openshift_ai` collection.** It does not exist yet. This
  repository is written against its announced interface and tested against a
  stub:
  - roles `preflight`, `receiver_image`, `publish`, `admit`, `serve`, `stories`,
    `teardown`;
  - `serveguard_predictor_cpu`/`_memory` in role defaults;
  - `serveguard_stories_result` = `[{story, expected, verdict, reason}]`;
  - the optional names in `serveguard_workload_vars` (`config.yml`).

  Reconcile those names, and the git subdirectory in `requirements.yml`, when it
  lands.
