# openshift-playbook

Modular Ansible playbooks for deploying GPU-accelerated LLM inference on ROSA HCP.

## Quickstart

```bash
git clone <repo-url> && cd openshift-playbook
ansible-galaxy collection install -r requirements.yml
ansible-vault create inventory/group_vars/all/vault.yml   # add OCM token + registry creds
rh-aws-saml-login                                          # authenticate AWS
ansible-playbook playbooks/bootstrap.yml --ask-vault-pass  # auto-discover infrastructure
ansible-playbook playbooks/llm-endpoint.yml --ask-vault-pass
```

See below for details on each step.

## Getting Started

### Prerequisites

- Python 3.10+
- Ansible 2.15+
- CLI tools: `aws`, `rosa`, `ocm`, `oc` (see `versions.yml` for minimum versions)
- AWS session (via `rh-aws-saml-login` or `aws sso login`)
- An [OCM API token](https://console.redhat.com/openshift/token)
- A [Red Hat registry service account](https://access.redhat.com/terms-based-registry/) (for pull secret)

### Setup

```bash
# 1. Clone and install dependencies
git clone <repo-url> && cd openshift-playbook
ansible-galaxy collection install -r requirements.yml

# 2. Create your vault (you'll be prompted for a vault password)
ansible-vault create inventory/group_vars/all/vault.yml
```

Paste the following into the vault editor — replace the placeholder values:

```yaml
---
vault_ocm_token: <your-ocm-token>
vault_registry_username: <your-rh-service-account>
vault_registry_password: <your-rh-service-account-password>
```

Where to get these:
- **OCM token**: https://console.redhat.com/openshift/token
- **Registry credentials**: https://access.redhat.com/terms-based-registry/

Optionally, save your vault password so you don't type it every run:

```bash
echo 'your-vault-password' > .vault_pass && chmod 600 .vault_pass
```

### Bootstrap (recommended)

The bootstrap playbook auto-discovers your infrastructure (OIDC config,
IAM roles, VPC subnets with tag headroom check) and writes `site.yml`:

```bash
# Authenticate AWS first (interactive, can't be automated)
rh-aws-saml-login        # or: aws sso login --profile <profile>

# Run bootstrap — discovers infrastructure, prompts for selections,
# writes inventory/group_vars/all/site.yml
ansible-playbook playbooks/bootstrap.yml --ask-vault-pass

# Or, set the cluster name upfront to skip the prompt
ansible-playbook playbooks/bootstrap.yml --ask-vault-pass \
  --extra-vars 'rosa_cluster_name=my-cluster'
```

Bootstrap is **idempotent** — re-running it only discovers values not yet
in `site.yml`. If `site.yml` already has all values, bootstrap does nothing.

What it discovers:
- **OIDC config** — lists existing configs, auto-selects or prompts; offers to create one if none exist
- **IAM roles** — finds HCP ROSA Installer/Support/Worker roles by name pattern
- **VPC subnets** — finds private/public pairs in the same AZ, filters by tag headroom (each machine pool uses ~8 of the 50 tag limit), presents ranked options

### Manual setup (alternative)

If you prefer to configure manually instead of running bootstrap:

```bash
cp inventory/group_vars/all/site.yml.example inventory/group_vars/all/site.yml
```

Then fill in each value. Use these commands to discover them:

```bash
# OIDC config
rosa list oidc-config
# If none exist: rosa create oidc-config --mode auto --managed

# IAM roles (look for HCP installer, support, worker)
rosa list account-roles | grep -i hcp
# If none exist: rosa create account-roles --mode auto --hosted-cp

# Subnets (need one private + one public in the same AZ)
aws ec2 describe-subnets --region us-east-1 \
  --query 'Subnets[*].[SubnetId, AvailabilityZone,
    Tags[?Key==`Name`].Value | [0], MapPublicIpOnLaunch]' \
  --output table

# Check subnet tag headroom (need at least 8 free of 50)
aws ec2 describe-tags \
  --filters "Name=resource-id,Values=<subnet-id>" \
  --query 'Tags | length(@)' --region us-east-1
```

### Verify setup

```bash
# Run preflight — validates CLI versions, AWS credentials, subnet tag
# headroom, GPU instance availability, operator catalog, and pull secret.
# Changes nothing; fails fast with a clear report if anything is wrong.
ansible-playbook playbooks/llm-endpoint-preflight.yml --ask-vault-pass
```

The preflight checks subnet tag headroom (each machine pool uses ~8 of
the 50 tag limit), GPU instance availability, operator catalog access,
and more. If anything fails, the error message tells you exactly what
to fix.

### Deploy

```bash
# Full deployment — creates cluster, GPU pool, operators, and serves a model
ansible-playbook playbooks/llm-endpoint.yml --ask-vault-pass

# Or, if you already have a cluster and just want the workload
ansible-playbook playbooks/llm-endpoint-on-cluster.yml --ask-vault-pass
```

That's it. The playbook will:
1. Log in to OCM/ROSA/oc (using your vault credentials)
2. Create the ROSA HCP cluster (or skip if it exists)
3. Grant OCM access roles
4. Create a GPU machine pool and wait for the node
5. Install operators (NFD, GPU, Serverless, ServiceMesh, RHOAI)
6. Deploy the model and wait for the inference endpoint

### Verify

```bash
# Check the endpoint
oc get inferenceservice -n default

# Test it
curl -sk "$(oc get inferenceservice mistral-small-24b -n default \
  -o jsonpath='{.status.url}')/v1/models"
```

### Teardown

```bash
# Remove workload (keeps cluster)
ansible-playbook playbooks/llm-endpoint-teardown.yml --ask-vault-pass

# Remove everything including cluster
ansible-playbook playbooks/llm-endpoint-teardown.yml --ask-vault-pass \
  -e teardown_cluster=true
```

## Design Principles

- **Declarative end state** — describe what you want, Ansible resolves the order
- **Dependency resolution** — roles declare dependencies; Ansible runs them in the right order
- **Preflight validation** — all constraints checked before any changes (like `uv` resolving before installing)
- **Overlays** — swap GPU type, model, cluster, access profile, or replica count via var files
- **Idempotent** — safe to re-run; only changes what's needed
- **Version-pinned** — `versions.yml` locks operator channels, instance types, cluster version, and OCM API versions
- **Schema-as-data** — OCM API field paths, response kinds, and role IDs live in `versions.yml`, not in task logic. Changing the API version or response schema is a data edit, not a code change

## Dependency Graph

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

`cluster_access` runs before `gpu_machinepool` to ensure OCM-level permissions
(cluster-admin, ClusterEditor, MachinePoolEditor) are in place.

## Overlays

Three overlay directories customize deployments without changing code:

| Directory | What it overrides | Example |
|-----------|-------------------|---------|
| `scenarios/` | Model, GPU type, replica count | `@scenarios/demo.yml` |
| `clusters/` | Cluster name, subnets, IAM, region | `@clusters/staging.yml` |
| `profiles/` | OCM access grants (users + roles) | `@profiles/my-team.yml` |

Overlays compose — use multiple `--extra-vars` to combine them:

```bash
# Deploy a demo scenario on a staging cluster with team access
ansible-playbook playbooks/llm-endpoint-on-cluster.yml \
  --extra-vars '@clusters/staging.yml' \
  --extra-vars '@scenarios/demo.yml' \
  --extra-vars '@profiles/my-team.yml' \
  --extra-vars 'registry_username=... registry_password=...'
```

### Scenarios

Swap model, GPU type, or scale:

```bash
ansible-playbook playbooks/llm-endpoint-on-cluster.yml --extra-vars '@scenarios/demo.yml' \
  --extra-vars 'registry_username=... registry_password=...'
```

### Cluster Profiles

Target a different ROSA cluster without touching inventory:

```bash
# Default cluster (from inventory/group_vars/all/config.yml)
ansible-playbook playbooks/llm-endpoint-on-cluster.yml ...

# Different cluster
ansible-playbook playbooks/llm-endpoint-on-cluster.yml --extra-vars '@clusters/staging.yml' ...
```

A cluster profile overrides cluster-specific variables (`rosa_cluster_name`,
subnets, IAM roles, region). See `clusters/example.yml` for the template.
You can also override `versions_file` to pin a different OpenShift version per cluster.

### Access Profiles

Grant OCM roles to multiple users in a single run:

```bash
# Default: current OCM user gets all roles
ansible-playbook playbooks/step-cluster-access.yml

# Single user override (legacy, still supported)
ansible-playbook playbooks/step-cluster-access.yml \
  --extra-vars 'cluster_access_username=other_user cluster_access_account_id=abc123'

# Multi-user profile
ansible-playbook playbooks/step-cluster-access.yml \
  --extra-vars '@profiles/my-team.yml'
```

A profile defines `cluster_access_grants` — a list of grants, each specifying
a user and which roles/groups they need:

```yaml
cluster_access_grants:
  - username: <ocm-username>
    account_id: <ocm-account-id>
    subscription_roles:       # keys from versions.ocm.subscription_roles
      - cluster_editor
      - machine_pool_editor
    groups:                    # keys from versions.ocm.groups
      - cluster_admins

  - username: <another-user>
    account_id: <another-account-id>
    subscription_roles:
      - machine_pool_editor
    groups: []                 # no cluster-admin needed
```

Grant specs reference **key names** (`cluster_editor`), not OCM role IDs
(`ClusterEditor`). The mapping lives in `versions.yml`, so renaming a role
in OCM requires updating one line.

## Individual Operations

```bash
# Bootstrap — auto-discover infrastructure and write site.yml
ansible-playbook playbooks/bootstrap.yml --ask-vault-pass

# Cluster only
ansible-playbook playbooks/rosa-cluster.yml --ask-vault-pass

# Grant OCM access roles
ansible-playbook playbooks/step-cluster-access.yml --ask-vault-pass

# GPU machine pool only (override instance type on the fly)
ansible-playbook playbooks/rosa-gpu-pool.yml --ask-vault-pass \
  --extra-vars 'gpu_instance_type=g6e.2xlarge'

# Individual workload steps
ansible-playbook playbooks/step-operators.yml --ask-vault-pass
ansible-playbook playbooks/step-gpu-stack.yml --ask-vault-pass
ansible-playbook playbooks/step-ai-platform.yml --ask-vault-pass
ansible-playbook playbooks/step-pull-secret.yml --ask-vault-pass
ansible-playbook playbooks/step-model.yml --ask-vault-pass

# Check for upgrades
ansible-playbook playbooks/rosa-upgrade.yml --ask-vault-pass

# Teardown workload (keeps cluster)
ansible-playbook playbooks/llm-endpoint-teardown.yml --ask-vault-pass

# Teardown everything including cluster
ansible-playbook playbooks/llm-endpoint-teardown.yml --ask-vault-pass \
  -e teardown_cluster=true
```

## Configuration

| File | Committed | Purpose |
|------|-----------|---------|
| `versions.yml` | yes | Version constraints — operators, cluster version, instance types, OCM API schema |
| `inventory/group_vars/all/config.yml` | yes | Public defaults — timeouts, GPU pool, model serving, versions path |
| `inventory/group_vars/all/site.yml` | **no** | Site-specific — cluster name, AWS account, subnets, IAM ARNs |
| `inventory/group_vars/all/site.yml.example` | yes | Template for `site.yml` |
| `inventory/group_vars/all/vault.yml` | **no** | Ansible Vault encrypted secrets (OCM tokens, passwords) |
| `scenarios/*.yml` | yes | Model/GPU overrides |
| `clusters/*.yml` | **no** | Cluster-specific overrides (contain account IDs, subnet IDs) |
| `clusters/example.yml` | yes | Template for cluster profiles |
| `profiles/*.yml` | **no** | Access grant profiles (contain OCM usernames, account IDs) |
| `profiles/example.yml` | yes | Template for access profiles |

### First-time setup

```bash
# 1. Create vault for secrets
ansible-vault create inventory/group_vars/all/vault.yml
# Add: vault_ocm_token, vault_registry_username, vault_registry_password

# 2. Run bootstrap to auto-discover infrastructure and write site.yml
ansible-playbook playbooks/bootstrap.yml --ask-vault-pass

# Or manually: copy template and fill in values
# cp inventory/group_vars/all/site.yml.example inventory/group_vars/all/site.yml

# 3. (Optional) Store vault password for convenience
echo 'your-vault-password' > .vault_pass
chmod 600 .vault_pass
```

All `site.yml`, `vault.yml`, `clusters/*.yml`, and `profiles/*.yml` are
gitignored — only `.example` templates are committed.

## Roles

| Role | Purpose | Dependencies |
|------|---------|-------------|
| `bootstrap` | Auto-discover infrastructure, write `site.yml` | login |
| `login` | Authenticate AWS, ROSA, OCM sessions | none |
| `preflight` | Validate all constraints before changes | none |
| `rosa_cluster` | Create/wait for ROSA HCP cluster | none |
| `cluster_access` | Grant OCM roles (supports multiple users via profiles) | login |
| `gpu_machinepool` | GPU machine pool with taint, wait for node | cluster_access |
| `operators` | Install NFD, GPU, Serverless, ServiceMesh, RHOAI; wait for CRDs | none |
| `gpu_stack` | NFD instance + ClusterPolicy; wait for GPU labels | operators, gpu_machinepool |
| `ai_platform` | DSCI + DataScienceCluster + KServe + mesh | operators |
| `pull_secret` | registry.redhat.io credentials | none |
| `model` | ServingRuntime + InferenceService; wait for Ready | gpu_stack, ai_platform, pull_secret |

## Architecture: Role Conventions

Every role follows a three-phase structure:

```
roles/<name>/tasks/
├── main.yml              # orchestrator: preconditions → deploy → postconditions
├── preconditions.yml     # assert the world is ready; set _deploy_needed
├── deploy.yml            # make changes (only runs when _deploy_needed)
├── postconditions.yml    # assert the desired end state holds
└── _*.yml                # private subtasks (included via loop, not called directly)
```

### `main.yml` — Orchestrator

Named task labels, conditional deploy, postconditions always run:

```yaml
---
- name: "<role>: check preconditions"
  ansible.builtin.include_tasks: preconditions.yml

- name: "<role>: deploy"
  ansible.builtin.include_tasks: deploy.yml
  when: _deploy_needed | default(true)

- name: "<role>: check postconditions"
  ansible.builtin.include_tasks: postconditions.yml
```

### `preconditions.yml` — Guard

- Load `versions.yml` if not already loaded
- Use `-o json` on every CLI call; never parse table output
- Validate response schemas (`kind` field) before extracting data
- Set `_deploy_needed` so deploy is skipped when the end state already holds
- Reference identifiers from `versions.ocm.*` — never hardcode role IDs, group names, or API versions

### `deploy.yml` — Mutate

- Guard every task with `when:` so only missing pieces are applied
- For OCM API responses, check structured JSON error fields (`kind`, `id`) instead of string matching on error messages
- CLI tools (e.g., `rosa`) that don't return structured errors are documented as exceptions

### `postconditions.yml` — Verify

- Re-query state after deploy (don't trust cached variables)
- Use `ansible.builtin.assert` with clear `fail_msg` referencing the specific identifier that failed
- Validate response schemas before extracting data

### Private Subtasks (`_*.yml`)

For roles that process lists (e.g., multiple access grants), the three phases
delegate to `_`-prefixed subtask files via `include_tasks` + loop:

```yaml
# preconditions.yml evaluates each grant
- name: Evaluate access grants
  ansible.builtin.include_tasks: _evaluate_grant.yml
  loop: "{{ cluster_access_grants }}"
  loop_control:
    loop_var: _grant
```

Each iteration is self-contained. Results accumulate via `set_fact` appending
to a list (e.g., `_evaluated_grants`). This keeps the three-phase convention
intact while supporting per-item processing.

### Step Playbooks

Minimal wrappers — no `connection`, `gather_facts`, or `vars_files`:

```yaml
---
# Step: <description>
#
# Usage:
#   ansible-playbook playbooks/step-<name>.yml

- name: <description>
  hosts: localhost
  roles:
    - role: login
    - role: <name>
```

## Architecture: Schema-as-Data

All OCM API interaction is governed by `versions.yml`. API versions, response
schemas, field paths, role identifiers, and group names are defined in one place
and consumed as variables — task files contain no hardcoded OCM identifiers.

### `versions.yml` — OCM section

```yaml
ocm:
  api:
    clusters_mgmt:
      version: v1
    accounts_mgmt:
      version: v1
      fields:                        # JSON field paths for selectattr/map
        account_id: account.id       # nested in v1; would be account_id in a flat schema
        role_id: role.id             # nested in v1; would be role_id in a flat schema
  schema:                            # expected 'kind' values in responses
    role_binding_list: SubscriptionRoleBindingList
    role_binding: RoleBinding
    account: Account
    error: Error
  subscription_roles:                # keys are profile identifiers; values are OCM IDs
    cluster_editor: ClusterEditor
    machine_pool_editor: MachinePoolEditor
  groups:
    cluster_admins:
      name: cluster-admins           # OCM group name in the API path
      user_id_field: id              # field path for user ID in group members response
```

**Why field paths matter**: `selectattr('account.id', ...)` works for OCM v1's
nested structure. If v2 flattens this to `account_id`, changing the field path
in `versions.yml` updates every `selectattr`/`map` call — zero task file edits.

### How roles consume it

```yaml
# 1. Load versions (idempotent — every role does this)
- name: Load version constraints
  ansible.builtin.include_vars:
    file: "{{ versions_file }}"
    name: versions
  when: versions is not defined

# 2. Derive API paths and field accessors from pinned versions
- name: Derive OCM API paths and field accessors
  ansible.builtin.set_fact:
    _ocm_accounts_api: "/api/accounts_mgmt/{{ versions.ocm.api.accounts_mgmt.version }}"
    _field_account_id: "{{ versions.ocm.api.accounts_mgmt.fields.account_id }}"
    _field_role_id: "{{ versions.ocm.api.accounts_mgmt.fields.role_id }}"

# 3. Validate response schema before extracting data
- name: Validate response schema
  ansible.builtin.assert:
    that: >-
      (_result.stdout | from_json).kind | default('')
        == versions.ocm.schema.role_binding_list

# 4. Extract data using version-driven field paths
- name: Parse role bindings for user
  ansible.builtin.set_fact:
    _user_role_ids: >-
      {{ items
         | selectattr(_field_account_id, 'equalto', _target_account_id)
         | map(attribute=_field_role_id) | list }}

# 5. Map profile keys to OCM IDs via versions
- name: Compute required role IDs
  ansible.builtin.set_fact:
    _required: >-
      {{ ['cluster_editor', 'machine_pool_editor']
         | map('extract', versions.ocm.subscription_roles) | list }}
    # → ['ClusterEditor', 'MachinePoolEditor']

# 6. Set difference to find what's missing
- name: Compute missing roles
  ansible.builtin.set_fact:
    _missing: "{{ _required | difference(_user_role_ids) }}"
```

### Error handling

For **OCM API** (`ocm post`), responses are structured JSON. Check the `kind`
and `id` fields instead of string matching:

```yaml
# OCM returns kind=RoleBinding on success, kind=Error with id=409 on conflict.
failed_when: >-
  _result.rc != 0 and
  ((_result.stdout | default('{}') | from_json).id | default('')) != '409'
changed_when: >-
  _result.rc == 0 and
  (_result.stdout | default('{}') | from_json).kind | default('')
    == versions.ocm.schema.role_binding
```

For **CLI tools** (`rosa`, `oc`) that don't return structured errors, string
matching on stderr is documented as an exception. Postconditions always provide
the authoritative verification via structured JSON.

## Extending: Adding a New Role That Queries OCM

1. **Add identifiers to `versions.yml`** if querying new endpoints, roles, or groups.

2. **Load versions** at the top of `preconditions.yml`:
   ```yaml
   - name: Load version constraints
     ansible.builtin.include_vars:
       file: "{{ versions_file }}"
       name: versions
     when: versions is not defined
   ```

3. **Derive API paths and field accessors** — never inline `/api/.../v1/...`
   or hardcode field names like `account.id`:
   ```yaml
   - name: Derive OCM API paths
     ansible.builtin.set_fact:
       _ocm_accounts_api: "/api/accounts_mgmt/{{ versions.ocm.api.accounts_mgmt.version }}"
       _field_role_id: "{{ versions.ocm.api.accounts_mgmt.fields.role_id }}"
   ```

4. **Validate schemas** — assert `kind` before extracting:
   ```yaml
   - name: Validate response
     ansible.builtin.assert:
       that: (_response.stdout | from_json).kind == versions.ocm.schema.expected_kind
   ```

5. **Use field path variables** in `selectattr`/`map`:
   ```yaml
   | selectattr(_field_role_id, 'equalto', versions.ocm.subscription_roles.machine_pool_editor)
   ```

6. **Create a step playbook** — `playbooks/step-<name>.yml` following the minimal convention.

7. **Add to pipeline playbooks** — insert the role in `llm-endpoint.yml` and/or
   `llm-endpoint-on-cluster.yml` at the correct position in the dependency chain.

## Extending: Supporting a New OCM API Version

If OCM releases a new API version (e.g., `accounts_mgmt/v2`):

1. Update `versions.yml` — bump the version and adjust field paths:
   ```yaml
   ocm:
     api:
       accounts_mgmt:
         version: v2
         fields:
           account_id: account_id   # flattened in v2
           role_id: role_id         # flattened in v2
   ```

2. If response `kind` values changed, update `versions.yml` `schema` entries.

3. All roles that use `versions.ocm.api.accounts_mgmt.version` and the field
   accessors will automatically pick up both changes — no per-file edits needed.

## Extending: Adding a New Cluster

1. Copy `clusters/example.yml` to `clusters/my-cluster.yml`.

2. Fill in the cluster-specific values (name, subnets, IAM roles, region).

3. Run any playbook against it:
   ```bash
   ansible-playbook playbooks/llm-endpoint-on-cluster.yml --extra-vars '@clusters/my-cluster.yml' ...
   ```

4. Optionally point to a different versions file per cluster:
   ```yaml
   versions_file: "{{ playbook_dir }}/../versions-staging.yml"
   ```

The default cluster (from `inventory/group_vars/all/config.yml`) is used
when no `--extra-vars '@clusters/...'` is passed — existing usage is unchanged.

## Extending: Adding a New Access Profile

1. Create `profiles/my-team.yml` with a `cluster_access_grants` list.

2. Each grant specifies **key names** from `versions.yml` (not raw OCM IDs):
   ```yaml
   cluster_access_grants:
     - username: someone
       account_id: abc123
       subscription_roles:
         - cluster_editor         # → versions.ocm.subscription_roles.cluster_editor
         - machine_pool_editor    # → versions.ocm.subscription_roles.machine_pool_editor
       groups:
         - cluster_admins         # → versions.ocm.groups.cluster_admins
   ```

3. Run:
   ```bash
   ansible-playbook playbooks/step-cluster-access.yml --extra-vars '@profiles/my-team.yml'
   ```

Without a profile, `cluster_access` defaults to the current OCM user
(from `ocm whoami`) with all roles and groups.

## Adding a New Scenario

Create `scenarios/my-scenario.yml`:

```yaml
model_name: my-model
model_storage_uri: "hf://org/model-name"
gpu_instance_type: g6e.2xlarge
gpu_pool_replicas: 1
```

Then run:

```bash
ansible-playbook playbooks/llm-endpoint-on-cluster.yml \
  --extra-vars '@scenarios/my-scenario.yml' \
  --extra-vars 'registry_username=... registry_password=...'
```

The same roles handle different models, GPU types, and scales — no code changes needed.
