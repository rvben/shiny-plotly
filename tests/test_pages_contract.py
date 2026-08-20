"""The two proofs the Pages workflow runs, held against each other.

`make site-check` judges the export built for a local URL; `make site-verify`
judges the deployed site, which is the one visitors get and the only one whose
wheel URL exists. So the live probe is the more important of the two, and it is
the easy one to leave thinner: it runs after a green deploy, against a site that
is already live, where a check that proves less still reads as a check.
"""

from __future__ import annotations

from pathlib import Path

MAKEFILE = Path(__file__).parent.parent / "Makefile"

# The programs that judge the demo, as each appears in a recipe line: a generic
# boot check, and the probe for what this app in particular has to do.
PROBES = ("shinylive-check", "tools/site_check.py")


def recipe(target: str) -> list[str]:
    """The command lines of `target`: the tab-indented lines that follow it."""
    lines: list[str] = []
    collecting = False
    for line in MAKEFILE.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{target}:"):
            collecting = True
        elif collecting:
            if line.startswith("\t"):
                lines.append(line)
            elif line.strip():
                break
    assert lines, f"the Makefile has no recipe for {target}"
    return lines


def probes(target: str) -> set[str]:
    return {probe for probe in PROBES if any(probe in line for line in recipe(target))}


def test_the_live_check_proves_everything_the_pre_deploy_check_does():
    before = probes("site-check")
    # Named, not derived: a parser that found nothing would make the comparison
    # below vacuously true, and a check that passes by finding nothing is the
    # failure this file is about.
    assert before == set(PROBES), f"site-check no longer runs {set(PROBES) - before}"
    assert not before - probes("site-verify"), (
        f"site-verify does not run {before - probes('site-verify')}, so what ships "
        "is proven less than what was built"
    )
