#!/usr/bin/env python3
"""Prove a root batch script receives its lane without shell interpolation."""
import json
import sys

import yaml

from test_script_gates import NODE_LIB, gate_script, run


def main():
    step = next(s for s in yaml.safe_load(NODE_LIB.read_text())["jobs"]["package"]["steps"] if s.get("name") == "Extra scripts")
    assert step["env"]["PACKAGE_MATRIX_JSON"] == "${{ toJSON(matrix) }}"
    lane = {"label": "batch-1", "filter": "", "packages": ["@example/one", "@example/two"], "extra-scripts": "ci:batch"}
    script = "node -e 'const p=JSON.parse(process.env.PACKAGE_MATRIX_JSON).packages;if(p.length!==2)process.exit(2);console.log(p.join(\",\"))'"
    for status in (0, 7):
        rc, output, _ = run(gate_script(NODE_LIB, "package", "Extra scripts"), {"ci:batch": script + f" && exit {status}"}, env_extra={
            "PACKAGE_MATRIX_JSON": json.dumps(lane), "LANE_SCRIPTS_JSON": json.dumps("ci:batch"),
            "LEGACY_SCRIPTS": "", "PM": "npm", "FILTER": "", "REQUIRE": "true", "GATE": "Extra scripts",
        })
        assert rc == status, output
        assert "@example/one,@example/two" in output, output
    print("PASS batch metadata reaches the declared root gate and its failure propagates")
    return 0


if __name__ == "__main__":
    sys.exit(main())
