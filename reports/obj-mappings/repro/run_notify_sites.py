"""Sweep runner for the notify-site reproducers.

    python run_notify_sites.py <script.py> <arg...> -- <build> [build...] [-n RUNS]

Reports exit status / signal per run and greps stderr for AddressSanitizer,
assertion failures, and negative-refcount aborts.
"""

import os
import subprocess
import sys

BUILDS = "/home/danzin/projects/python_build_matrix/builds"


def main(argv):
    if "--" not in argv:
        print(__doc__)
        return 2
    cut = argv.index("--")
    script_and_args = argv[:cut]
    rest = argv[cut + 1:]
    runs = 5
    if "-n" in rest:
        i = rest.index("-n")
        runs = int(rest[i + 1])
        rest = rest[:i] + rest[i + 2:]
    builds = rest

    for build in builds:
        exe = os.path.join(BUILDS, build, "python")
        if not os.path.exists(exe):
            print("MISSING BUILD %s" % build)
            continue
        env = dict(os.environ)
        if "asan" in build:
            env["PYTHONMALLOC"] = "malloc"
            env["ASAN_OPTIONS"] = env.get(
                "ASAN_OPTIONS", "detect_leaks=0:abort_on_error=0:handle_abort=1"
            )
        outcomes = {}
        first_detail = {}
        for _ in range(runs):
            p = subprocess.run(
                [exe] + script_and_args,
                env=env,
                capture_output=True,
                timeout=600,
            )
            err = p.stderr.decode("utf-8", "replace")
            out = p.stdout.decode("utf-8", "replace")
            tag = "exit %d" % p.returncode
            if p.returncode < 0:
                tag = "SIGNAL %d" % (-p.returncode)
            if "AddressSanitizer" in err:
                for line in err.splitlines():
                    if "ERROR: AddressSanitizer" in line:
                        tag = "ASAN " + line.split("AddressSanitizer:")[1].strip()[:60]
                        break
                else:
                    tag = "ASAN (unparsed) " + tag
            elif "Assertion" in err and "failed" in err:
                for line in err.splitlines():
                    if "Assertion" in line:
                        tag = "ASSERT " + line.strip()[-90:]
                        break
            elif "Fatal Python error" in err:
                for line in err.splitlines():
                    if "Fatal Python error" in line:
                        tag = "FATAL " + line.strip()[:90]
                        break
            outcomes[tag] = outcomes.get(tag, 0) + 1
            first_detail.setdefault(tag, (out, err))
        print("=== %s : %s" % (build, " ".join(script_and_args)))
        for tag, n in sorted(outcomes.items(), key=lambda kv: -kv[1]):
            print("    %d/%d  %s" % (n, runs, tag))
        for tag in outcomes:
            if tag.startswith(("ASAN", "ASSERT", "SIGNAL", "FATAL")):
                out, err = first_detail[tag]
                print("    ---- stdout ----")
                print("\n".join("    " + x for x in out.splitlines()[-8:]))
                print("    ---- stderr ----")
                print("\n".join("    " + x for x in err.splitlines()[:45]))
                break
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
