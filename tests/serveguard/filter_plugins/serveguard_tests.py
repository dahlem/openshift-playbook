"""Test-only filters for tests/serveguard."""


def sg_test_role_includes(tasks):
    """include_role tasks (also inside blocks) -> ["<role>/tasks/<entry>"] in order."""
    out = []
    for t in tasks or []:
        if not isinstance(t, dict):
            continue
        for key in ("block", "always", "rescue"):
            out += sg_test_role_includes(t.get(key))
        inc = t.get("ansible.builtin.include_role") or t.get("include_role")
        if isinstance(inc, dict) and str(inc.get("name", "")).startswith("serveguard.openshift_ai."):
            role = inc["name"].split(".")[-1]
            out.append(f"{role}/tasks/{inc.get('tasks_from', 'main')}".removesuffix(".yml"))
    return out


class FilterModule:
    def filters(self):
        return {"sg_test_role_includes": sg_test_role_includes}
