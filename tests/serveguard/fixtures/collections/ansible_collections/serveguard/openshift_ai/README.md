Minimal stub of serveguard.openshift_ai for openshift-playbook's offline wiring test
(tests/serveguard/test_workload.yml). It has the real collection's role entry points
(test_interface.yml checks that against ../serveguard) and result facts, and no
workload values: those are read from the real collection's roles/common defaults.
