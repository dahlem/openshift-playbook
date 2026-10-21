#!/usr/bin/env bash
# Offline tests for the ServeGuard playbooks: no cloud, no cluster, no login.
#   tests/serveguard/run.sh
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
cd "$repo"
# The test inventory has no vault; do not require .vault_pass.
export ANSIBLE_CONFIG="$here/ansible.cfg"
export ANSIBLE_ROLES_PATH="$repo/roles"
# A stub serveguard.openshift_ai collection stands in for the real one.
export ANSIBLE_COLLECTIONS_PATH="$here/fixtures/collections"
log="${TMPDIR:-/tmp}/serveguard-tests.log"
run() {
  if ansible-playbook -i "$here/inventory" "$here/$1" -e versions_file="$repo/versions.yml" "${@:2}" >"$log" 2>&1; then
    echo "PASS $1: $(grep -oE 'ok=[0-9]+ +changed=[0-9]+ +unreachable=0 +failed=0' "$log" | head -1)$(grep -oE '[0-9]+ decision cases passed' "$log" | sed 's/^/, /')"
  else
    echo "FAIL $1 (full log: $log)"; grep -E -A30 'fatal:|ERROR' "$log" | head -60; exit 1
  fi
}
run test_checks.yml
run test_preflight.yml
run test_identity.yml
run test_workload.yml
# The real collection, from the sibling checkout, linked like an installed one.
real="$repo/../serveguard/integrations/openshift-ai/ansible"
if [ -f "$real/galaxy.yml" ]; then
  coll="$(mktemp -d)"; trap 'rm -rf "$coll"' EXIT
  mkdir -p "$coll/ansible_collections/serveguard"
  ln -s "$(cd "$real" && pwd)" "$coll/ansible_collections/serveguard/openshift_ai"
  export SERVEGUARD_COLLECTION_DIR="$(cd "$real" && pwd)"
  ANSIBLE_COLLECTIONS_PATH="$coll" run test_interface.yml
  ANSIBLE_COLLECTIONS_PATH="$coll" run test_interface.yml -e serveguard_predictor_cpu=6 -e expect_override=6
else
  echo "SKIP test_interface.yml (no ../serveguard checkout with the collection)"
fi
echo "== syntax-check"
# With the repository's own ansible.cfg and inventory, as a user runs them.
for p in playbooks/*.yml; do
  ANSIBLE_CONFIG="$repo/ansible.cfg" ansible-playbook --syntax-check -e @scenarios/serveguard/cpu.yml "$p" >"$log" 2>&1 \
    || { echo "FAIL syntax-check $p"; cat "$log"; exit 1; }
done
echo "all playbooks pass --syntax-check"
