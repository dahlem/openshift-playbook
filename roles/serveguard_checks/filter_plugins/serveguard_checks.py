"""Decision logic for the ServeGuard pre- and postcondition checks.

Every ``sg_eval_*`` filter is pure: it takes what the gather tasks read from
rosa / aws / oc (as parsed JSON) plus the requirements, and returns

    {"rows": [{"step", "check", "status", "detail"}, ...], "action": <str>, ...}

Status values:
    ok         a precondition holds
    satisfied  the step's end state already holds; the step is skipped
    todo       missing; the step will create it
    wait       exists and is converging; the step will wait for it
    override   conflicting, accepted because an override variable is set
    warn       not blocking, worth reading
    n/a        cannot be checked yet (an earlier step has not run)
    conflict   exists in a conflicting state; blocking
    fail       a precondition does not hold; blocking

Keeping the logic here lets tests/serveguard run it on fixture JSON offline.
"""

from __future__ import annotations

import re
from typing import Any

BLOCKING = ("fail", "conflict")

_CPU = re.compile(r"^([0-9.]+)(m?)$")
_BYTES = re.compile(r"^([0-9.]+)([EPTGMK]i?|[eE][0-9]+|m)?$")
_UNITS = {
    "": 1,
    "K": 10**3,
    "M": 10**6,
    "G": 10**9,
    "T": 10**12,
    "P": 10**15,
    "E": 10**18,
    "Ki": 2**10,
    "Mi": 2**20,
    "Gi": 2**30,
    "Ti": 2**40,
    "Pi": 2**50,
    "Ei": 2**60,
}
_NOT_READY_TAINTS = ("node.kubernetes.io/not-ready", "node.kubernetes.io/unreachable")


# ---- quantities and versions ------------------------------------------------------


def k8s_cpu(value: Any) -> int:
    """CPU quantity -> millicores ("500m" -> 500, "4" -> 4000)."""
    if value is None or value == "":
        return 0
    m = _CPU.match(str(value).strip())
    if not m:
        raise ValueError(f"not a CPU quantity: {value!r}")
    number = float(m.group(1))
    return int(round(number if m.group(2) else number * 1000))


def k8s_bytes(value: Any) -> int:
    """Memory/storage quantity -> bytes ("8Gi" -> 8589934592)."""
    if value is None or value == "":
        return 0
    text = str(value).strip()
    m = _BYTES.match(text)
    if not m:
        raise ValueError(f"not a byte quantity: {value!r}")
    number, unit = float(m.group(1)), m.group(2) or ""
    if unit == "m":
        return int(number / 1000)
    if unit[:1] in "eE" and unit[1:].isdigit():
        return int(number * 10 ** int(unit[1:]))
    return int(number * _UNITS[unit])


def _gib(n: int) -> str:
    return f"{n / 2**30:.1f}Gi"


def _cores(m: int) -> str:
    return f"{m / 1000:g}"


def _vtuple(text: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", text)[:3])


def version_at_least(found: str | None, minimum: str | None) -> bool:
    if found is None:
        return False
    if minimum is None:
        return True
    a, b = _vtuple(found), _vtuple(minimum)
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) >= b + (0,) * (width - len(b))


def version_in_range(found: str, minimum: str, max_exclusive: str) -> bool:
    return version_at_least(found, minimum) and not version_at_least(found, max_exclusive)


_VERSION_PATTERNS = {
    "rosa": r"(\d+\.\d+\.\d+)",
    "aws": r"aws-cli/(\d+\.\d+\.\d+)",
    "oc": r"Client Version:\s*v?(\d+\.\d+\.\d+)",
    "ocm": r"(\d+\.\d+\.\d+)",
    "make": r"GNU Make (\d+\.\d+(?:\.\d+)?)",
    "uv": r"uv (\d+\.\d+\.\d+)",
}


def cli_version(tool: str, output: str | None) -> str | None:
    if not output:
        return None
    m = re.search(_VERSION_PATTERNS.get(tool, r"(\d+\.\d+(?:\.\d+)?)"), output)
    return m.group(1) if m else None


# ---- helpers ----------------------------------------------------------------------


def _row(step: str, check: str, status: str, detail: str = "") -> dict[str, str]:
    return {"step": step, "check": check, "status": status, "detail": detail}


def _blocking(rows: list[dict[str, str]]) -> bool:
    return any(r["status"] in BLOCKING for r in rows)


def _ready(node: dict[str, Any]) -> bool:
    for c in node.get("status", {}).get("conditions", []) or []:
        if c.get("type") == "Ready":
            return c.get("status") == "True"
    return False


def _conditions(obj: dict[str, Any] | None) -> list[dict[str, Any]]:
    return ((obj or {}).get("status") or {}).get("conditions") or []


def _condition_true(obj: dict[str, Any] | None, kind: str) -> bool:
    return any(c.get("type") == kind and c.get("status") == "True" for c in _conditions(obj))


def _annotations(obj: dict[str, Any] | None) -> dict[str, str]:
    return ((obj or {}).get("metadata") or {}).get("annotations") or {}


def ec2_family(instance_type: str) -> str:
    letter = instance_type[:1].lower()
    if letter in "acdhimrtz":
        return "standard"
    if letter in ("g", "v"):
        return "g_vt"
    if letter == "p":
        return "p"
    return "other"


# ---- local tools ------------------------------------------------------------------


def result_ok(raw: dict[str, Any], tool: str) -> bool:
    return tool != "ansible_core" and ((raw.get("tools") or {}).get(tool) or {}).get("rc") == 0


def sg_eval_local(raw: dict[str, Any], req: dict[str, Any]) -> dict[str, Any]:
    """raw: {tool: {"rc": int, "out": str}}, "ansible_core": str, "python_modules": {name: bool}."""
    step = "local"
    rows = []
    for tool, minimum in (req.get("cli") or {}).items():
        if tool == "ansible_core":
            found = raw.get("ansible_core")
        else:
            result = (raw.get("tools") or {}).get(tool) or {}
            found = cli_version(tool, result.get("out")) if result.get("rc") == 0 else None
            if result.get("rc") == 0 and found is None and minimum is None:
                found = "present"
        if found is None and result_ok(raw, tool):
            rows.append(_row(step, tool, "warn", f"installed, version unreadable (need >= {minimum})"))
        elif found is None:
            rows.append(_row(step, tool, "fail", f"not found on PATH (need >= {minimum})" if minimum else "not found on PATH"))
        elif minimum is not None and not version_at_least(found, minimum):
            rows.append(_row(step, tool, "fail", f"{found} < required {minimum}"))
        else:
            rows.append(_row(step, tool, "ok", found + (f" (>= {minimum})" if minimum else "")))
    for module, present in (raw.get("python_modules") or {}).items():
        rows.append(
            _row(step, f"python: {module}", "ok" if present else "fail",
                 "importable" if present else f"pip install {module} (for {raw.get('python', 'the ansible python')})")
        )
    return {"rows": rows, "action": "none"}


# ---- AWS account (bootstrap's postcondition) ----------------------------------------


def sg_eval_aws(raw: dict[str, Any], req: dict[str, Any]) -> dict[str, Any]:
    """raw: identity, oidc_configs, roles{kind: {arn, exists}}, elb_role, subnets, tag_count.
    req: oidc_config_id, private_subnet, public_subnet, region, min_tag_headroom."""
    step = "aws"
    rows = []
    identity = raw.get("identity")
    if not identity:
        return {"rows": [_row(step, "session", "fail", "aws sts get-caller-identity failed: run rh-aws-saml-login / aws sso login")],
                "action": "none"}
    rows.append(_row(step, "session", "ok", f"{identity.get('Arn', '?')} in {req.get('region')}"))
    if not raw.get("rosa_logged_in"):
        rows.append(_row(step, "rosa login", "fail", "rosa whoami failed: rosa login --token=<OCM token>"))
    else:
        rows.append(_row(step, "rosa login", "ok", raw.get("rosa_account", "")))

    missing_site = [k for k in ("oidc_config_id", "private_subnet", "public_subnet") if not req.get(k)]
    missing_site += [f"{k} role" for k, v in (raw.get("roles") or {}).items() if not v.get("arn")]
    if missing_site:
        rows.append(_row(step, "site.yml", "fail",
                         "missing " + ", ".join(missing_site) + ": run playbooks/bootstrap.yml"))

    oidc_ids = [c.get("id") for c in raw.get("oidc_configs") or []]
    if req.get("oidc_config_id"):
        if req["oidc_config_id"] in oidc_ids:
            rows.append(_row(step, "OIDC config", "ok", req["oidc_config_id"]))
        else:
            rows.append(_row(step, "OIDC config", "fail",
                             f"{req['oidc_config_id']} not in rosa list oidc-config: rosa create oidc-config --mode auto --managed, then re-run bootstrap"))

    for kind, role in sorted((raw.get("roles") or {}).items()):
        if not role.get("arn"):
            continue
        if role.get("exists"):
            rows.append(_row(step, f"IAM {kind} role", "ok", role["arn"].split("/")[-1]))
        else:
            rows.append(_row(step, f"IAM {kind} role", "fail",
                             f"{role['arn']} not found: rosa create account-roles --mode auto --hosted-cp"))

    if raw.get("elb_role"):
        rows.append(_row(step, "ELB service-linked role", "ok", "AWSServiceRoleForElasticLoadBalancing"))
    else:
        rows.append(_row(step, "ELB service-linked role", "fail",
                         "aws iam create-service-linked-role --aws-service-name elasticloadbalancing.amazonaws.com"))

    subnets = {s.get("SubnetId"): s for s in raw.get("subnets") or []}
    for kind in ("private", "public"):
        sid = req.get(f"{kind}_subnet")
        if sid and sid not in subnets:
            rows.append(_row(step, f"{kind} subnet", "fail", f"{sid} not found in {req.get('region')}"))
        elif sid:
            s = subnets[sid]
            public = bool(s.get("MapPublicIpOnLaunch"))
            if kind == "public" and not public:
                rows.append(_row(step, f"{kind} subnet", "warn", f"{sid} does not map public IPs; is it the public subnet?"))
            else:
                rows.append(_row(step, f"{kind} subnet", "ok", f"{sid} ({s.get('AvailabilityZone')}, {s.get('VpcId')})"))
    priv, pub = subnets.get(req.get("private_subnet")), subnets.get(req.get("public_subnet"))
    if priv and pub:
        if priv.get("VpcId") != pub.get("VpcId"):
            rows.append(_row(step, "subnet pair", "fail", f"private and public subnets are in different VPCs ({priv.get('VpcId')}, {pub.get('VpcId')})"))
        elif priv.get("AvailabilityZone") != pub.get("AvailabilityZone"):
            rows.append(_row(step, "subnet pair", "warn", "private and public subnets are in different AZs"))
    if raw.get("tag_count") is not None and req.get("private_subnet"):
        headroom = 50 - int(raw["tag_count"])
        need = int(req.get("min_tag_headroom", 8))
        rows.append(_row(step, "subnet tag headroom", "ok" if headroom >= need else "fail",
                         f"{raw['tag_count']}/50 tags on {req['private_subnet']}, need {need} free"))
    return {"rows": rows, "action": "none", "az": (priv or {}).get("AvailabilityZone")}


# ---- AWS quotas and instance types --------------------------------------------------


def sg_eval_quota(raw: dict[str, Any], req: dict[str, Any]) -> dict[str, Any]:
    """raw: az, offered[types], rosa_types[types]|None, specs{type: {vcpus, memory_mib, gpus}},
            quotas{code: float|None}, instances[{type, vcpus}].
    req: new_nodes[{role, type, count}] (nodes this run would add), quota_codes,
         per-node demand for the serving pool (predictor+job), node_overhead, accelerator."""
    step = "quota"
    rows = []
    az = raw.get("az")
    types = sorted({n["type"] for n in req.get("nodes") or []})
    for t in types:
        if az is None:
            rows.append(_row(step, f"{t} offered", "n/a", "subnet AZ unknown"))
        elif t in (raw.get("offered") or []):
            rows.append(_row(step, f"{t} offered", "ok", f"in {az}"))
        else:
            rows.append(_row(step, f"{t} offered", "fail", f"not offered in {az}; pick another type or a subnet in another AZ"))
        rosa_types = raw.get("rosa_types")
        if rosa_types is None:
            rows.append(_row(step, f"{t} supported by ROSA", "warn", "rosa list instance-types unavailable"))
        elif t not in rosa_types:
            rows.append(_row(step, f"{t} supported by ROSA", "fail", "not in rosa list instance-types"))

    specs = raw.get("specs") or {}
    need: dict[str, int] = {}
    for n in req.get("new_nodes") or []:
        vcpus = (specs.get(n["type"]) or {}).get("vcpus")
        if vcpus is None:
            rows.append(_row(step, f"{n['type']} vCPUs", "fail", "aws ec2 describe-instance-types returned nothing"))
            continue
        fam = ec2_family(n["type"])
        need[fam] = need.get(fam, 0) + int(vcpus) * int(n["count"])
    in_use: dict[str, int] = {}
    for i in raw.get("instances") or []:
        fam = ec2_family(i["type"])
        in_use[fam] = in_use.get(fam, 0) + int(i.get("vcpus") or 0)
    codes = req.get("quota_codes") or {}
    for fam, needed in sorted(need.items()):
        code = codes.get(fam)
        quota = (raw.get("quotas") or {}).get(code)
        label = {"standard": "Standard (A,C,D,H,I,M,R,T,Z)", "g_vt": "G and VT", "p": "P"}.get(fam, fam)
        if quota is None:
            rows.append(_row(step, f"vCPU quota {label}", "warn", f"quota {code} unreadable; need {needed} new vCPUs"))
            continue
        free = int(float(quota)) - in_use.get(fam, 0)
        status = "ok" if free >= needed else "fail"
        detail = f"quota {int(float(quota))}, in use {in_use.get(fam, 0)}, free {free}, this run adds {needed}"
        if status == "fail":
            detail += (f"; aws service-quotas request-service-quota-increase --service-code ec2"
                       f" --quota-code {code} --desired-value {in_use.get(fam, 0) + needed}")
        rows.append(_row(step, f"vCPU quota {label}", status, detail))

    # Plan check: will the serving node type hold the predictor and a co-located Job?
    pool = req.get("serving_type")
    demand = req.get("serving_demand") or {}
    if pool and pool in specs:
        s = specs[pool]
        overhead = req.get("node_overhead") or {}
        cpu = int(s["vcpus"]) * 1000 - k8s_cpu(overhead.get("cpu"))
        mem = int(s["memory_mib"]) * 2**20 - k8s_bytes(overhead.get("memory"))
        want_cpu, want_mem = k8s_cpu(demand.get("cpu")), k8s_bytes(demand.get("memory"))
        ok = cpu >= want_cpu and mem >= want_mem
        rows.append(_row(step, f"{pool} holds predictor+Job", "ok" if ok else "fail",
                         f"~{_cores(cpu)} CPU / {_gib(mem)} usable vs {_cores(want_cpu)} CPU / {_gib(want_mem)} needed"
                         + ("" if ok else "; choose a larger serving_pool_instance_type")))
        if req.get("accelerator") == "nvidia" and int(s.get("gpus") or 0) < 1:
            rows.append(_row(step, f"{pool} has a GPU", "fail", "accelerator nvidia needs a GPU instance type"))
    return {"rows": rows, "action": "none"}


# ---- cluster ------------------------------------------------------------------------

_CONVERGING = ("pending", "installing", "validating", "waiting")


def sg_eval_cluster(raw: dict[str, Any], req: dict[str, Any]) -> dict[str, Any]:
    """raw: exists, cluster (rosa describe cluster -o json).
    req: name, pinned, min, max_exclusive, region, must_exist, allow_version."""
    step = "cluster"
    rows = []
    lo, hi = req["min"], req["max_exclusive"]
    if not raw.get("exists"):
        if req.get("must_exist"):
            return {"rows": [_row(step, "exists", "fail", f"cluster {req['name']} not found; use playbooks/serveguard-demo.yml to create it")],
                    "action": "none"}
        if not version_in_range(req["pinned"], lo, hi):
            rows.append(_row(step, "pinned version", "fail",
                             f"versions.yml pins {req['pinned']}, outside {lo} <= v < {hi} for RHOAI"))
        else:
            rows.append(_row(step, "exists", "todo", f"will create {req['name']} (OpenShift {req['pinned']}, HCP)"))
        return {"rows": rows, "action": "create"}

    c = raw.get("cluster") or {}
    state = (c.get("status") or {}).get("state", "unknown")
    version = c.get("openshift_version") or (c.get("version") or {}).get("raw_id") or "unknown"
    hcp = bool((c.get("hypershift") or {}).get("enabled"))
    region = (c.get("region") or {}).get("id")
    action = "satisfied"
    if not hcp:
        rows.append(_row(step, "hosted control plane", "conflict",
                         f"{req['name']} is ROSA classic; these playbooks manage HCP machine pools"))
    if region and req.get("region") and region != req["region"]:
        rows.append(_row(step, "region", "conflict", f"cluster is in {region}, aws_region is {req['region']}"))
    if state == "ready":
        pass
    elif state in _CONVERGING:
        rows.append(_row(step, "state", "wait", f"{state}; the rosa_cluster step waits for ready"))
        action = "wait"
    else:
        rows.append(_row(step, "state", "conflict", f"state {state}; fix or delete the cluster first (rosa logs install -c {req['name']})"))
    if version == "unknown" or not version_in_range(version, lo, hi):
        if req.get("allow_version"):
            rows.append(_row(step, "version", "override", f"{version} outside {lo} <= v < {hi}; serveguard_allow_cluster_version=true"))
        else:
            rows.append(_row(step, "version", "conflict",
                             f"{version} outside {lo} <= v < {hi} for RHOAI; upgrade (playbooks/rosa-upgrade.yml) or set serveguard_allow_cluster_version=true"))
    if not _blocking(rows) and action == "satisfied":
        rows.append(_row(step, "ready", "satisfied", f"{req['name']} ready, OpenShift {version}"))
    return {"rows": rows, "action": "none" if _blocking(rows) else action,
            "api_url": (c.get("api") or {}).get("url"), "version": version}


# ---- machine pool -------------------------------------------------------------------


def _taints(pool: dict[str, Any]) -> list[str]:
    return sorted(f"{t.get('key')}={t.get('value', '')}:{t.get('effect')}" for t in pool.get("taints") or [])


def sg_eval_pool(raw: dict[str, Any], req: dict[str, Any]) -> dict[str, Any]:
    """raw: cluster_exists, exists, pool (rosa describe machinepool -o json), nodes [k8s Node].
    req: name, instance_type, autoscaling, min, max, replicas, taints [str], labels {k: v}, subnet, allow_mismatch."""
    step = "pool"
    name = req["name"]
    if not raw.get("cluster_exists"):
        return {"rows": [_row(step, name, "todo", f"will create {req['instance_type']} after the cluster")], "action": "create"}
    if not raw.get("exists"):
        return {"rows": [_row(step, name, "todo", f"will create {req['instance_type']}")], "action": "create"}
    pool = raw.get("pool") or {}
    diffs = []
    found_type = (pool.get("aws_node_pool") or {}).get("instance_type")
    if found_type != req["instance_type"]:
        diffs.append(f"instance type {found_type} != {req['instance_type']}")
    auto = pool.get("autoscaling") or {}
    if req.get("autoscaling"):
        got = (auto.get("min_replica"), auto.get("max_replica"))
        if not auto or got != (req["min"], req["max"]):
            diffs.append(f"autoscaling {got if auto else 'off'} != ({req['min']}, {req['max']})")
    elif pool.get("replicas") != req.get("replicas"):
        diffs.append(f"replicas {pool.get('replicas')} != {req.get('replicas')}")
    if _taints(pool) != sorted(req.get("taints") or []):
        diffs.append(f"taints {_taints(pool) or 'none'} != {sorted(req.get('taints') or []) or 'none'}")
    labels = pool.get("labels") or {}
    for k, v in (req.get("labels") or {}).items():
        if labels.get(k) != v:
            diffs.append(f"label {k}={labels.get(k)} != {v}")
    if req.get("subnet") and pool.get("subnet") and pool["subnet"] != req["subnet"]:
        diffs.append(f"subnet {pool['subnet']} != {req['subnet']}")
    if diffs:
        if req.get("allow_mismatch"):
            return {"rows": [_row(step, name, "override", "; ".join(diffs) + "; using it as is (serveguard_allow_pool_mismatch)")],
                    "action": "accept"}
        return {"rows": [_row(step, name, "conflict", "; ".join(diffs)
                              + f". Delete it (rosa delete machinepool {name} -c <cluster>), change serveguard_pool_name, or set serveguard_allow_pool_mismatch=true")],
                "action": "none"}
    ready = [n for n in raw.get("nodes") or [] if _ready(n)]
    if ready:
        return {"rows": [_row(step, name, "satisfied", f"{found_type}, {len(ready)} node(s) Ready")], "action": "satisfied"}
    return {"rows": [_row(step, name, "wait", f"{found_type} exists, no Ready node yet")], "action": "wait"}


# ---- operators ----------------------------------------------------------------------


def _og_mode(spec: dict[str, Any] | None) -> tuple[str, ...]:
    targets = (spec or {}).get("targetNamespaces") or []
    return tuple(sorted(targets))


def sg_eval_operator(raw: dict[str, Any], req: dict[str, Any]) -> dict[str, Any]:
    """One operator. raw: cluster_reachable, package (PackageManifest)|None, operator_groups [OG],
    subscription|None, other_subscriptions [Subscription for the same package elsewhere], csv|None.
    req: key, package, channel, source, namespace, sub_name, og_spec (None: namespace has its own),
    allow_channel_change."""
    step = f"operator {req['key']}"
    rows = []
    if not raw.get("cluster_reachable"):
        return {"rows": [_row(step, "catalog", "n/a", "cluster not reachable yet")], "action": "install",
                "adopt_og": False}
    pkg = raw.get("package")
    if not pkg:
        rows.append(_row(step, "catalog", "fail", f"package {req['package']} not in openshift-marketplace ({req['source']})"))
    else:
        channels = [c.get("name") for c in (pkg.get("status") or {}).get("channels") or []]
        if req["channel"] in channels:
            rows.append(_row(step, "channel", "ok", f"{req['package']} {req['channel']}"))
        else:
            rows.append(_row(step, "channel", "fail",
                             f"{req['channel']} not offered by {req['package']}; available: {', '.join(channels)}"))

    for other in raw.get("other_subscriptions") or []:
        md = other.get("metadata") or {}
        rows.append(_row(step, "foreign Subscription", "conflict",
                         f"{md.get('namespace')}/{md.get('name')} already subscribes to {req['package']}; remove it or reuse that install"))

    adopt = False
    ogs = raw.get("operator_groups") or []
    if req.get("og_spec") is not None:
        want = _og_mode(req["og_spec"])
        if len(ogs) > 1:
            names = ", ".join((o.get("metadata") or {}).get("name", "?") for o in ogs)
            rows.append(_row(step, "OperatorGroup", "conflict",
                             f"{len(ogs)} OperatorGroups in {req['namespace']} ({names}); OLM installs nothing until one is deleted"))
        elif len(ogs) == 1:
            og = ogs[0]
            og_name = (og.get("metadata") or {}).get("name")
            if _og_mode(og.get("spec")) != want:
                rows.append(_row(step, "OperatorGroup", "conflict",
                                 f"{og_name} targets {list(_og_mode(og.get('spec'))) or 'all namespaces'}, need {list(want) or 'all namespaces'}"))
            elif og_name != req["namespace"]:
                adopt = True
                rows.append(_row(step, "OperatorGroup", "ok", f"reusing compatible {og_name}"))
            else:
                rows.append(_row(step, "OperatorGroup", "ok", og_name))
        else:
            rows.append(_row(step, "OperatorGroup", "todo", f"will create {req['namespace']}"))

    sub = raw.get("subscription")
    action = "install"
    if sub is None:
        rows.append(_row(step, "Subscription", "todo", f"will subscribe {req['package']} on {req['channel']}"))
    else:
        spec = sub.get("spec") or {}
        if spec.get("name") != req["package"] or spec.get("source") != req["source"]:
            rows.append(_row(step, "Subscription", "conflict",
                             f"{req['sub_name']} subscribes {spec.get('name')} from {spec.get('source')}, need {req['package']} from {req['source']}"))
        elif spec.get("channel") != req["channel"]:
            if req.get("allow_channel_change"):
                rows.append(_row(step, "Subscription", "override",
                                 f"channel {spec.get('channel')} -> {req['channel']} (serveguard_allow_channel_change)"))
                action = "change_channel"
            else:
                rows.append(_row(step, "Subscription", "conflict",
                                 f"on channel {spec.get('channel')}, requested {req['channel']}; set serveguard_allow_channel_change=true to move it (upgrades in place)"))
        else:
            csv = raw.get("csv")
            phase = ((csv or {}).get("status") or {}).get("phase")
            installed = (sub.get("status") or {}).get("installedCSV")
            if phase == "Succeeded":
                rows.append(_row(step, "Subscription", "satisfied", f"{installed} Succeeded on {req['channel']}"))
                action = "satisfied"
            elif phase == "Failed":
                reason = ((csv or {}).get("status") or {}).get("message", "")
                rows.append(_row(step, "Subscription", "conflict", f"CSV {installed} Failed: {reason[:120]}"))
            else:
                rows.append(_row(step, "Subscription", "wait", f"CSV {installed or '(none yet)'} phase {phase or 'unknown'}"))
                action = "wait"
    return {"rows": rows, "action": "none" if _blocking(rows) else action, "adopt_og": adopt}


def sg_operator_raw(reqs, reachable, pkgs, ogs, subs, csvs):
    """Assemble sg_eval_operator inputs from the looped k8s_info results (parallel to reqs)."""
    def first(results, i):
        if i >= len(results or []):
            return None
        res = results[i].get("resources") or []
        return res[0] if res else None

    out = []
    for i, q in enumerate(reqs):
        ours = [s for s in subs or [] if s["metadata"]["namespace"] == q["namespace"] and s["metadata"]["name"] == q["sub_name"]]
        others = [s for s in subs or [] if (s.get("spec") or {}).get("name") == q["package"] and s not in ours]
        out.append({
            "cluster_reachable": reachable,
            "package": first(pkgs, i),
            "operator_groups": ((ogs or [])[i].get("resources") or []) if i < len(ogs or []) else [],
            "subscription": ours[0] if ours else None,
            "other_subscriptions": others,
            "csv": first(csvs, i),
        })
    return out


def sg_eval_operators(raws: list[dict[str, Any]], reqs: list[dict[str, Any]]) -> dict[str, Any]:
    results = [sg_eval_operator(r, q) for r, q in zip(raws, reqs)]
    rows = [row for res in results for row in res["rows"]]
    subset = [q["key"] for q, res in zip(reqs, results) if res["action"] in ("install", "wait", "change_channel")]
    return {
        "rows": rows,
        "action": "none" if _blocking(rows) else ("satisfied" if not subset else "install"),
        "subset": subset,
        "existing_operator_groups": [q["namespace"] for q, res in zip(reqs, results) if res["adopt_og"]],
        "apply_channel_change": any(res["action"] == "change_channel" for res in results),
    }


# ---- GPU stack (gpu scenario) ---------------------------------------------------------


def sg_eval_gpu_stack(raw: dict[str, Any], req: dict[str, Any]) -> dict[str, Any]:
    """raw: cluster_reachable, nodes [Node of the serving pool], cluster_policy|None. req: instance_type."""
    step = "gpu stack"
    if not raw.get("cluster_reachable"):
        return {"rows": [_row(step, "nvidia.com/gpu", "n/a", "cluster not reachable yet")], "action": "install"}
    nodes = raw.get("nodes") or []
    if not any(_ready(n) for n in nodes):
        return {"rows": [_row(step, "GPU node", "fail", f"no Ready {req['instance_type']} node; the pool step did not finish")],
                "action": "none"}
    gpus = sum(int(((n.get("status") or {}).get("allocatable") or {}).get("nvidia.com/gpu") or 0) for n in nodes)
    state = ((raw.get("cluster_policy") or {}).get("status") or {}).get("state")
    if gpus >= 1 and state == "ready":
        return {"rows": [_row(step, "nvidia.com/gpu", "satisfied", f"{gpus} allocatable, ClusterPolicy ready")], "action": "satisfied"}
    return {"rows": [_row(step, "nvidia.com/gpu", "todo", f"{gpus} allocatable, ClusterPolicy {state or 'absent'}")], "action": "install"}


# ---- DSCInitialization / DataScienceCluster -------------------------------------------


def sg_eval_platform(raw: dict[str, Any], req: dict[str, Any]) -> dict[str, Any]:
    """raw: cluster_reachable, dsc_crd, dscs [DSC], dscis [DSCI], kserve (Deployment)|None.
    req: dsc_name, dsci_name, kserve {managementState, nim{...}}, removed [names], allow_update, rhoai_pending."""
    step = "platform"
    rows = []
    if not raw.get("cluster_reachable") or (not raw.get("dsc_crd") and req.get("rhoai_pending")):
        return {"rows": [_row(step, "DataScienceCluster", "n/a", "RHOAI not installed yet")], "action": "create"}
    if not raw.get("dsc_crd"):
        return {"rows": [_row(step, "DSC CRD", "fail", "datascienceclusters CRD missing; the operators step did not finish")], "action": "none"}

    dscis = raw.get("dscis") or []
    if len(dscis) > 1:
        rows.append(_row(step, "DSCInitialization", "conflict", f"{len(dscis)} DSCInitializations; only one is allowed"))
    elif dscis and (dscis[0].get("metadata") or {}).get("name") != req["dsci_name"]:
        rows.append(_row(step, "DSCInitialization", "conflict",
                         f"{dscis[0]['metadata']['name']} exists, the platform step expects {req['dsci_name']}"))
    elif dscis:
        rows.append(_row(step, "DSCInitialization", "ok", req["dsci_name"]))
    else:
        rows.append(_row(step, "DSCInitialization", "todo", "the operator creates it (or the platform step does)"))

    dscs = raw.get("dscs") or []
    others = [d for d in dscs if (d.get("metadata") or {}).get("name") != req["dsc_name"]]
    for d in others:
        rows.append(_row(step, "DataScienceCluster", "conflict",
                         f"{d['metadata']['name']} exists; only one DSC per cluster. Reuse it (rename dsc) or delete it"))
    ours = next((d for d in dscs if (d.get("metadata") or {}).get("name") == req["dsc_name"]), None)
    action = "create"
    if ours is None:
        rows.append(_row(step, "DataScienceCluster", "todo", f"will create {req['dsc_name']} (KServe standard only)"))
    else:
        comps = (ours.get("spec") or {}).get("components") or {}
        diffs = []
        want_kserve = req["kserve"]
        got_kserve = comps.get("kserve") or {}
        if got_kserve.get("managementState") != want_kserve.get("managementState"):
            diffs.append(f"kserve {got_kserve.get('managementState', 'unset')}")
        want_nim = (want_kserve.get("nim") or {}).get("managementState")
        if want_nim and (got_kserve.get("nim") or {}).get("managementState") != want_nim:
            diffs.append(f"kserve.nim {(got_kserve.get('nim') or {}).get('managementState', 'unset')}")
        for name in req["removed"]:
            state = (comps.get(name) or {}).get("managementState", "unset")
            if state != "Removed":
                diffs.append(f"{name} {state}")
        phase = (ours.get("status") or {}).get("phase")
        if diffs:
            detail = "components differ: " + ", ".join(diffs)
            if req.get("allow_update"):
                rows.append(_row(step, "DataScienceCluster", "override", detail + " -> will set (serveguard_allow_dsc_update)"))
                action = "update"
            else:
                rows.append(_row(step, "DataScienceCluster", "conflict",
                                 detail + ". It may serve other workloads; set serveguard_allow_dsc_update=true to change it"))
        elif phase == "Ready":
            kserve = raw.get("kserve") or {}
            if int((kserve.get("status") or {}).get("readyReplicas") or 0) >= 1:
                rows.append(_row(step, "DataScienceCluster", "satisfied", f"{req['dsc_name']} Ready, KServe standard"))
                action = "satisfied"
            else:
                rows.append(_row(step, "DataScienceCluster", "wait", "Ready, kserve-controller-manager not ready"))
                action = "wait"
        else:
            rows.append(_row(step, "DataScienceCluster", "wait", f"components as required, phase {phase or 'unknown'}"))
            action = "wait"
    return {"rows": rows, "action": "none" if _blocking(rows) else action}


# ---- integrated image registry --------------------------------------------------------


def sg_eval_registry(raw: dict[str, Any], req: dict[str, Any]) -> dict[str, Any]:
    """raw: cluster_reachable, config (imageregistry Config cluster)|None, deployment|None. req: needed."""
    step = "registry"
    if not req.get("needed"):
        return {"rows": [_row(step, "internal registry", "n/a", "serveguard_image set; no in-cluster build")], "action": "none"}
    if not raw.get("cluster_reachable"):
        return {"rows": [_row(step, "internal registry", "n/a", "cluster not reachable yet")], "action": "none"}
    rows = []
    cfg = raw.get("config")
    state = ((cfg or {}).get("spec") or {}).get("managementState")
    if state != "Managed":
        rows.append(_row(step, "managementState", "fail",
                         f"{state or 'absent'}; enable it (oc patch configs.imageregistry.operator.openshift.io cluster "
                         "--type merge -p '{\"spec\":{\"managementState\":\"Managed\"}}') or set serveguard_image"))
    else:
        storage = ((cfg or {}).get("status") or {}).get("storage") or {}
        rows.append(_row(step, "managementState", "ok", "Managed, storage " + (", ".join(storage) or "unset")))
        if not storage:
            rows.append(_row(step, "storage", "fail", "registry has no storage configured"))
    dep = raw.get("deployment") or {}
    if int((dep.get("status") or {}).get("readyReplicas") or 0) >= 1:
        rows.append(_row(step, "image-registry deployment", "ok", f"{dep['status']['readyReplicas']} ready"))
    elif state == "Managed":
        rows.append(_row(step, "image-registry deployment", "fail", "no ready replica in openshift-image-registry"))
    return {"rows": rows, "action": "none"}


# ---- storage ----------------------------------------------------------------------------

_NO_RWX = ("ebs.csi.aws.com", "kubernetes.io/aws-ebs")


def sg_eval_storage(raw: dict[str, Any], req: dict[str, Any]) -> dict[str, Any]:
    """raw: cluster_reachable, storage_classes [SC], pvc|None. req: storage_class, size, access_mode, pvc_name."""
    step = "storage"
    if not raw.get("cluster_reachable"):
        return {"rows": [_row(step, "StorageClass", "n/a", "cluster not reachable yet")], "action": "none"}
    rows = []
    classes = raw.get("storage_classes") or []
    if req.get("storage_class"):
        sc = next((c for c in classes if c["metadata"]["name"] == req["storage_class"]), None)
        if sc is None:
            rows.append(_row(step, "StorageClass", "fail", f"{req['storage_class']} not found"))
    else:
        defaults = [c for c in classes if _annotations(c).get("storageclass.kubernetes.io/is-default-class") == "true"]
        sc = defaults[0] if defaults else None
        if not defaults:
            rows.append(_row(step, "StorageClass", "fail", "no default StorageClass; set serveguard_storage_class"))
        elif len(defaults) > 1:
            rows.append(_row(step, "StorageClass", "warn", f"{len(defaults)} defaults; using {defaults[0]['metadata']['name']}"))
    if sc is not None:
        prov = sc.get("provisioner", "")
        if prov == "kubernetes.io/no-provisioner":
            rows.append(_row(step, "StorageClass", "fail", f"{sc['metadata']['name']} has no dynamic provisioner"))
        elif req.get("access_mode") == "ReadWriteMany" and prov in _NO_RWX:
            rows.append(_row(step, "StorageClass", "fail", f"{sc['metadata']['name']} ({prov}) cannot provide ReadWriteMany"))
        else:
            rows.append(_row(step, "StorageClass", "ok", f"{sc['metadata']['name']} ({prov})"))
    pvc = raw.get("pvc")
    if pvc is not None:
        cap = ((pvc.get("status") or {}).get("capacity") or {}).get("storage") or \
            (((pvc.get("spec") or {}).get("resources") or {}).get("requests") or {}).get("storage")
        modes = (pvc.get("spec") or {}).get("accessModes") or []
        phase = (pvc.get("status") or {}).get("phase")
        problems = []
        if k8s_bytes(cap) < k8s_bytes(req["size"]):
            problems.append(f"capacity {cap} < {req['size']}")
        if req["access_mode"] not in modes:
            problems.append(f"access modes {modes} lack {req['access_mode']}")
        if phase not in ("Bound", "Pending"):
            problems.append(f"phase {phase}")
        if problems:
            rows.append(_row(step, f"PVC {req['pvc_name']}", "conflict", "; ".join(problems) + "; delete it or run the demo teardown"))
        else:
            rows.append(_row(step, f"PVC {req['pvc_name']}", "ok", f"reusing {cap} {phase}"))
    else:
        rows.append(_row(step, f"PVC {req['pvc_name']}", "todo", f"demo-up creates {req['size']} {req['access_mode']}"))
    return {"rows": rows, "action": "none"}


# ---- cluster capacity -------------------------------------------------------------------


def _pod_requests(pod: dict[str, Any]) -> dict[str, int]:
    spec = pod.get("spec") or {}

    def total(containers: list[dict[str, Any]]) -> dict[str, int]:
        out = {"cpu": 0, "memory": 0, "gpu": 0}
        for c in containers or []:
            r = ((c.get("resources") or {}).get("requests")) or ((c.get("resources") or {}).get("limits")) or {}
            out["cpu"] += k8s_cpu(r.get("cpu"))
            out["memory"] += k8s_bytes(r.get("memory"))
            out["gpu"] += int(r.get("nvidia.com/gpu") or 0)
        return out

    main = total(spec.get("containers") or [])
    for init in spec.get("initContainers") or []:
        one = total([init])
        for k in main:
            main[k] = max(main[k], one[k])
    overhead = spec.get("overhead") or {}
    main["cpu"] += k8s_cpu(overhead.get("cpu"))
    main["memory"] += k8s_bytes(overhead.get("memory"))
    return main


def sg_node_free(nodes: list[dict[str, Any]], pods: list[dict[str, Any]], exclude_namespaces: list[str] | None = None) -> list[dict[str, Any]]:
    """Free requests per node. Pods in exclude_namespaces are ignored (the demo's own pods,
    which a redeploy replaces)."""
    exclude = set(exclude_namespaces or [])
    used: dict[str, dict[str, int]] = {}
    for pod in pods or []:
        node = (pod.get("spec") or {}).get("nodeName")
        phase = (pod.get("status") or {}).get("phase")
        if not node or phase in ("Succeeded", "Failed") or (pod.get("metadata") or {}).get("namespace") in exclude:
            continue
        r = _pod_requests(pod)
        u = used.setdefault(node, {"cpu": 0, "memory": 0, "gpu": 0})
        for k in u:
            u[k] += r[k]
    out = []
    for n in nodes or []:
        name = n["metadata"]["name"]
        alloc = (n.get("status") or {}).get("allocatable") or {}
        blocking_taints = [
            t for t in (n.get("spec") or {}).get("taints") or []
            if t.get("effect") in ("NoSchedule", "NoExecute") and t.get("key") not in _NOT_READY_TAINTS
        ]
        u = used.get(name, {"cpu": 0, "memory": 0, "gpu": 0})
        out.append({
            "name": name,
            "instance_type": ((n.get("metadata") or {}).get("labels") or {}).get("node.kubernetes.io/instance-type"),
            "schedulable": _ready(n) and not (n.get("spec") or {}).get("unschedulable") and not blocking_taints,
            "taints": [f"{t.get('key')}:{t.get('effect')}" for t in blocking_taints],
            "free_cpu": k8s_cpu(alloc.get("cpu")) - u["cpu"],
            "free_memory": k8s_bytes(alloc.get("memory")) - u["memory"],
            "free_gpu": int(alloc.get("nvidia.com/gpu") or 0) - u["gpu"],
            "ephemeral": k8s_bytes(alloc.get("ephemeral-storage")),
        })
    return out


def sg_eval_capacity(raw: dict[str, Any], req: dict[str, Any]) -> dict[str, Any]:
    """raw: cluster_reachable, nodes [Node], pods [Pod].
    req: namespace, demands [{name, cpu, memory, gpu, min_ephemeral}], pool_pending (the serving
    pool has no Ready node yet, so a shortfall is reported as n/a instead of fail)."""
    step = "capacity"
    if not raw.get("cluster_reachable"):
        return {"rows": [_row(step, "allocatable", "n/a", "cluster not reachable yet (see the quota plan check)")], "action": "none"}
    free = sg_node_free(raw.get("nodes") or [], raw.get("pods") or [], [req.get("namespace")])
    usable = [n for n in free if n["schedulable"]]
    rows = [_row(step, "schedulable nodes", "ok" if usable else "fail",
                 f"{len(usable)} of {len(free)} (untainted, Ready)" if usable else "no Ready, untainted, schedulable node")]
    for d in req.get("demands") or []:
        cpu, mem, gpu = k8s_cpu(d.get("cpu")), k8s_bytes(d.get("memory")), int(d.get("gpu") or 0)
        eph = k8s_bytes(d.get("min_ephemeral"))
        fits = [n for n in usable
                if n["free_cpu"] >= cpu and n["free_memory"] >= mem and n["free_gpu"] >= gpu and n["ephemeral"] >= eph]
        want = f"{_cores(cpu)} CPU / {_gib(mem)}" + (f" / {gpu} GPU" if gpu else "") + (f" / {_gib(eph)} ephemeral" if eph else "")
        if fits:
            best = max(fits, key=lambda n: (n["free_cpu"], n["free_memory"]))
            rows.append(_row(step, d["name"], "ok",
                             f"{want} fits on {best['name']} (free {_cores(best['free_cpu'])} CPU / {_gib(best['free_memory'])})"))
        else:
            top = max(usable, key=lambda n: (n["free_gpu"] >= gpu, n["free_cpu"], n["free_memory"]), default=None)
            most = (f"; most free: {top['name']} {_cores(top['free_cpu'])} CPU / {_gib(top['free_memory'])}"
                    + (f" / {top['free_gpu']} GPU" if gpu else "") + f" / {_gib(top['ephemeral'])} ephemeral") if top else ""
            if req.get("pool_pending"):
                rows.append(_row(step, d["name"], "n/a",
                                 f"no node has {want} free yet; the serving pool is still to be created (see the quota plan check)"))
            else:
                rows.append(_row(step, d["name"], "fail", f"no schedulable node has {want} free{most}"))
    return {"rows": rows, "action": "none"}


# ---- demo -------------------------------------------------------------------------------

_VERDICT = ("serveguard.io/adapter-digest", "serveguard.io/package-root", "serveguard.io/base-hash")


def _isvc_gpu(isvc: dict[str, Any]) -> bool:
    model = (((isvc.get("spec") or {}).get("predictor") or {}).get("model")) or {}
    res = model.get("resources") or {}
    return any("nvidia.com/gpu" in (res.get(k) or {}) for k in ("limits", "requests"))


def sg_eval_demo(raw: dict[str, Any], req: dict[str, Any]) -> dict[str, Any]:
    """raw: checkout, makefile_params [names], cluster_reachable, isvc|None, isvcs [ISVC in namespace].
    req: namespace, isvc, accelerator, redeploy, predictor_cpu/memory, predictor_defaults, make_params."""
    step = "demo"
    rows = []
    if not raw.get("checkout"):
        rows.append(_row(step, "serveguard checkout", "fail",
                         f"{req['integration_dir']}/Makefile not found; clone serveguard next to this repo or set serveguard_repo_dir"))
    else:
        rows.append(_row(step, "serveguard checkout", "ok", req["integration_dir"]))
        params = set(raw.get("makefile_params") or [])
        missing = [p for p in ("NAMESPACE", "ISVC", "ACCELERATOR", "VLLM_IMAGE", "MODEL", "MODEL_REVISION", "TIMEOUT") if p not in params]
        if missing:
            rows.append(_row(step, "Makefile parameters", "fail", "Makefile lacks " + ", ".join(missing) + "; update the serveguard checkout"))
        cpu_p, mem_p = req["make_params"]["cpu"], req["make_params"]["memory"]
        custom = (k8s_cpu(req["predictor_cpu"]) != k8s_cpu(req["predictor_defaults"]["cpu"])
                  or k8s_bytes(req["predictor_memory"]) != k8s_bytes(req["predictor_defaults"]["memory"]))
        if cpu_p in params and mem_p in params:
            rows.append(_row(step, "predictor size", "ok", f"{req['predictor_cpu']} CPU / {req['predictor_memory']} via {cpu_p}/{mem_p}"))
        elif custom:
            rows.append(_row(step, "predictor size", "fail",
                             f"Makefile has no {cpu_p}/{mem_p}; the predictor keeps the integration default "
                             f"{req['predictor_defaults']['cpu']} CPU / {req['predictor_defaults']['memory']}. Update serveguard or reset serveguard_predictor_*"))
        else:
            rows.append(_row(step, "predictor size", "ok", f"integration default {req['predictor_cpu']} CPU / {req['predictor_memory']}"))
    if not raw.get("cluster_reachable"):
        rows.append(_row(step, "InferenceService", "n/a", "cluster not reachable yet"))
        return {"rows": rows, "action": "none" if _blocking(rows) else "deploy"}

    isvc = raw.get("isvc")
    leftovers = [i["metadata"]["name"] for i in raw.get("isvcs") or [] if i["metadata"]["name"].startswith("story-")]
    if leftovers:
        rows.append(_row(step, "story leftovers", "warn", f"{', '.join(leftovers)} exist: a refusal story was accepted earlier"))
    action = "deploy"
    if isvc is None:
        rows.append(_row(step, "InferenceService", "todo", f"demo-up creates {req['namespace']}/{req['isvc']}"))
    else:
        ann = _annotations(isvc)
        admitted = ann.get("serveguard.io/admitted") == "true" and all(ann.get(k) for k in _VERDICT)
        ready = _condition_true(isvc, "Ready")
        wrong_accel = _isvc_gpu(isvc) != (req["accelerator"] == "nvidia")
        if req.get("redeploy"):
            rows.append(_row(step, "InferenceService", "override", "serveguard_redeploy=true: publish and admit again"))
        elif not ann.get("serveguard.io/admitted"):
            rows.append(_row(step, "InferenceService", "conflict",
                             f"{req['isvc']} exists without a ServeGuard verdict; it was not created by the receiver. Delete it or use another serveguard_namespace"))
        elif wrong_accel:
            rows.append(_row(step, "InferenceService", "conflict",
                             f"{req['isvc']} was admitted for the other accelerator; run the teardown for that scenario or set serveguard_redeploy=true"))
        elif admitted and ready:
            rows.append(_row(step, "InferenceService", "satisfied", f"{req['isvc']} admitted and Ready"))
            action = "satisfied"
        else:
            rows.append(_row(step, "InferenceService", "conflict",
                             f"{req['isvc']} admitted={ann.get('serveguard.io/admitted')} Ready={ready}. A story may have left the adapter tampered: "
                             f"make -C {req['integration_dir']} story-restore NAMESPACE={req['namespace']}, or set serveguard_redeploy=true"))
    return {"rows": rows, "action": "none" if _blocking(rows) else action}


def sg_eval_demo_post(raw: dict[str, Any], req: dict[str, Any]) -> dict[str, Any]:
    """raw: isvc, vap, binding, pods [predictor pods]. Postcondition of demo-up (and of the stories)."""
    step = "demo"
    rows = []
    isvc = raw.get("isvc")
    ann = _annotations(isvc)
    if isvc is None:
        rows.append(_row(step, "InferenceService", "fail", f"{req['namespace']}/{req['isvc']} not found"))
    else:
        missing = [k for k in ("serveguard.io/admitted",) + _VERDICT if not ann.get(k)]
        if ann.get("serveguard.io/admitted") != "true" or missing:
            rows.append(_row(step, "verdict", "fail", "missing/false: " + ", ".join(missing or ["serveguard.io/admitted"])))
        else:
            rows.append(_row(step, "verdict", "ok", f"adapter-digest {ann['serveguard.io/adapter-digest'][:12]}"))
        if _condition_true(isvc, "Ready"):
            rows.append(_row(step, "InferenceService Ready", "ok", req["isvc"]))
        else:
            bad = [f"{c.get('type')}={c.get('status')} {c.get('reason', '')}".strip()
                   for c in _conditions(isvc) if c.get("status") != "True"]
            rows.append(_row(step, "InferenceService Ready", "fail", "; ".join(bad)[:200] or "no conditions"))
        if req.get("expect_digest") and ann.get("serveguard.io/adapter-digest") != req["expect_digest"]:
            rows.append(_row(step, "verdict unchanged", "fail",
                             f"adapter-digest changed {req['expect_digest'][:12]} -> {ann.get('serveguard.io/adapter-digest', '')[:12]}"))
        elif req.get("expect_digest"):
            rows.append(_row(step, "verdict unchanged", "ok", "same adapter-digest as before the stories"))
    for kind in ("vap", "binding"):
        label = "ValidatingAdmissionPolicy" if kind == "vap" else "ValidatingAdmissionPolicyBinding"
        rows.append(_row(step, label, "ok" if raw.get(kind) else "fail",
                         f"serveguard-admitted-adapters-{req['namespace']}" + ("" if raw.get(kind) else " not found")))
    pods = raw.get("pods") or []
    running = [p for p in pods if (p.get("status") or {}).get("phase") == "Running"
               and all(cs.get("ready") for cs in (p.get("status") or {}).get("containerStatuses") or [])]
    guard_ok = any(
        ((ic.get("state") or {}).get("terminated") or {}).get("exitCode") == 0
        for p in running for ic in (p.get("status") or {}).get("initContainerStatuses") or []
        if ic.get("name") == "serveguard-guard"
    )
    if running and guard_ok:
        rows.append(_row(step, "predictor pod", "ok", f"{running[0]['metadata']['name']} Running, guard exited 0"))
    else:
        states = [f"{p['metadata']['name']}: {(p.get('status') or {}).get('phase')}" for p in pods]
        rows.append(_row(step, "predictor pod", "fail", "; ".join(states) or "no predictor pod"))
    if req.get("refused_names") is not None:
        accepted = [i["metadata"]["name"] for i in raw.get("isvcs") or [] if i["metadata"]["name"] in req["refused_names"]]
        rows.append(_row(step, "refused services absent", "fail" if accepted else "ok",
                         ("exist although refused: " + ", ".join(accepted)) if accepted else ", ".join(req["refused_names"])))
        jobs = {j["metadata"]["name"]: j for j in raw.get("jobs") or []}
        for name in req.get("refused_jobs") or []:
            job = jobs.get(name)
            failed = job is not None and _condition_true(job, "Failed")
            rows.append(_row(step, f"Job {name}", "ok" if failed else "fail",
                             "Failed (receiver refused)" if failed else ("absent" if job is None else "did not fail")))
    return {"rows": rows, "action": "none"}


def sg_command_results(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """A looped command's registered results -> {label: {"rc", "out", "json"}}.

    The label is the loop item (or item.key for dict2items loops). "json" is the
    parsed stdout, or None. A missing executable counts as rc 127."""
    import json

    out = {}
    for r in results or []:
        item = r.get("item")
        label = item.get("key") if isinstance(item, dict) and "key" in item else str(item)
        text = (r.get("stdout") or "") + (("\n" + r["stderr"]) if r.get("stderr") else "")
        try:
            parsed = json.loads(r.get("stdout") or "")
        except ValueError:
            parsed = None
        out[label] = {"rc": int(r.get("rc", 127)) if r.get("rc") is not None else 127, "out": text, "json": parsed}
    return out


def sg_role_presence(arns: dict[str, str], results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """{kind: arn} + looped `aws iam get-role` results -> {kind: {"arn", "exists"}}."""
    found = sg_command_results(results)
    return {kind: {"arn": arn, "exists": (found.get(kind) or {}).get("rc") == 0} for kind, arn in arns.items()}


def sg_quota_value(result: dict[str, Any]) -> float | None:
    try:
        return float((result.get("stdout") or "").strip()) if result.get("rc") == 0 else None
    except ValueError:
        return None


def sg_instance_vcpus(instances: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"type": i.get("type"), "vcpus": int(i.get("cores") or 0) * int(i.get("threads") or 1)} for i in instances or []]


def sg_rc_ok(results: dict[str, dict[str, Any]]) -> dict[str, bool]:
    return {k: v.get("rc") == 0 for k, v in (results or {}).items()}


def sg_operator_diag(namespace: str) -> list[str]:
    """Read-only diagnostics for one operator namespace."""
    return [
        f"oc get operatorgroup,subscription,installplan,csv -n {namespace} -o wide",
        f"oc describe subscription -n {namespace} | sed -n '/Conditions:/,/Events:/p'",
        f"oc get csv -n {namespace} -o jsonpath='{{range .items[*]}}{{.metadata.name}} {{.status.phase}} {{.status.reason}}: {{.status.message}}{{\"\\n\"}}{{end}}'",
        f"oc get events -n {namespace} --field-selector type=Warning --sort-by=.lastTimestamp",
    ]


def sg_merge_rows(existing: list[dict[str, str]], new: list[dict[str, str]]) -> list[dict[str, str]]:
    """Append new rows; a re-check of the same step/check replaces the earlier row in place."""
    out = list(existing or [])
    for row in new or []:
        key = (row["step"], row["check"])
        idx = next((i for i, r in enumerate(out) if (r["step"], r["check"]) == key), None)
        if idx is None:
            out.append(row)
        else:
            out[idx] = row
    return out


def serveguard_row(row: dict[str, str]) -> str:
    return f"{row['step'][:21]:<21} {row['check'][:38]:<38} {row['status'].upper():<9} {row['detail']}"


class FilterModule:
    def filters(self) -> dict[str, Any]:
        return {
            "serveguard_row": serveguard_row,
            "sg_merge_rows": sg_merge_rows,
            "sg_command_results": sg_command_results,
            "sg_rc_ok": sg_rc_ok,
            "sg_operator_diag": sg_operator_diag,
            "sg_quota_value": sg_quota_value,
            "sg_instance_vcpus": sg_instance_vcpus,
            "sg_role_presence": sg_role_presence,
            "k8s_cpu": k8s_cpu,
            "k8s_bytes": k8s_bytes,
            "sg_cli_version": cli_version,
            "sg_version_in_range": version_in_range,
            "sg_node_free": sg_node_free,
            "sg_eval_local": sg_eval_local,
            "sg_eval_aws": sg_eval_aws,
            "sg_eval_quota": sg_eval_quota,
            "sg_eval_cluster": sg_eval_cluster,
            "sg_eval_pool": sg_eval_pool,
            "sg_eval_operators": sg_eval_operators,
            "sg_operator_raw": sg_operator_raw,
            "sg_eval_gpu_stack": sg_eval_gpu_stack,
            "sg_eval_platform": sg_eval_platform,
            "sg_eval_registry": sg_eval_registry,
            "sg_eval_storage": sg_eval_storage,
            "sg_eval_capacity": sg_eval_capacity,
            "sg_eval_demo": sg_eval_demo,
            "sg_eval_demo_post": sg_eval_demo_post,
        }
