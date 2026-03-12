# Architecture

## Role Conventions

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

## Schema-as-Data

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

## Extending

### Adding a New Role That Queries OCM

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

### Supporting a New OCM API Version

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

### Adding a New Cluster

1. Copy `clusters/example.yml` to `clusters/my-cluster.yml`.
2. Fill in the cluster-specific values (name, subnets, IAM roles, region).
3. Run any playbook against it:
   ```bash
   ansible-playbook playbooks/llm-endpoint-on-cluster.yml -e @clusters/my-cluster.yml ...
   ```
4. Optionally point to a different versions file per cluster:
   ```yaml
   versions_file: "{{ playbook_dir }}/../versions-staging.yml"
   ```

### Adding a New Access Profile

1. Create `profiles/my-team.yml` with a `cluster_access_grants` list.
2. Each grant specifies **key names** from `versions.yml` (not raw OCM IDs):
   ```yaml
   cluster_access_grants:
     - username: someone
       account_id: abc123
       subscription_roles:
         - cluster_editor
         - machine_pool_editor
       groups:
         - cluster_admins
   ```
3. Run:
   ```bash
   ansible-playbook playbooks/step-cluster-access.yml -e @profiles/my-team.yml
   ```

### Adding a New Scenario

Create `scenarios/my-scenario.yml`:

```yaml
model_name: my-model
model_storage_uri: "hf://org/model-name"
gpu_instance_type: g6e.2xlarge
gpu_pool_min_replicas: 2
gpu_pool_max_replicas: 8
```

Then run:

```bash
ansible-playbook playbooks/llm-endpoint-on-cluster.yml -e @scenarios/my-scenario.yml --ask-vault-pass
```

The same roles handle different models, GPU types, and scales — no code changes needed.
