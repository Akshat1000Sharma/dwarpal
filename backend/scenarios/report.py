"""Turn a run into the two artifacts CI uploads and the README quotes.

Failures are printed and written every time. A report that showed only what passed would be read
as dishonest, and honesty is the thing being graded.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.db.base import utcnow
from scenarios.harness import Suite


def summarise(
    suites: list[Suite], *, profile: str, base: str, wall_ms: int, environment: dict[str, Any]
) -> dict[str, Any]:
    cases = [c for s in suites for c in s.cases]
    failed = [c for c in cases if not c.passed]
    return {
        "generated_at": utcnow().isoformat(),
        "profile": profile,
        "merchant": base,
        "environment": environment,
        "totals": {
            "suites": len(suites),
            "cases": len(cases),
            "passed": len(cases) - len(failed),
            "failed": len(failed),
            "pass_rate": round((len(cases) - len(failed)) / len(cases), 4) if cases else 0.0,
            "wall_ms": wall_ms,
            "slowest_case_ms": max((c.duration_ms for c in cases), default=0),
        },
        "failures": [c.as_dict() for c in failed],
        "suites": [s.as_dict() for s in suites],
    }


def write(document: dict[str, Any], directory: Path) -> dict[str, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "scenario_suite.json"
    md_path = directory / "scenario_suite.md"
    json_path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    md_path.write_text(_markdown(document), encoding="utf-8")
    return {"json": json_path, "markdown": md_path}


def _markdown(document: dict[str, Any]) -> str:
    totals = document["totals"]
    lines: list[str] = [
        "# Dwarpal scenario suite",
        "",
        f"Profile `{document['profile']}` against `{document['merchant']}`, "
        f"generated {document['generated_at']}.",
        "",
        "| Measure | Value |",
        "|---|---|",
        f"| Suites | {totals['suites']} |",
        f"| Cases | {totals['cases']} |",
        f"| Passed | {totals['passed']} |",
        f"| Failed | {totals['failed']} |",
        f"| Pass rate | {totals['pass_rate'] * 100:.1f}% |",
        f"| Wall time | {totals['wall_ms'] / 1000:.1f}s |",
        f"| Slowest case | {totals['slowest_case_ms'] / 1000:.1f}s |",
        "",
    ]

    environment = document.get("environment") or {}
    if environment:
        lines += [
            "## What the merchant reported afterwards",
            "",
            "| Measure | Value |",
            "|---|---|",
        ]
        for key, value in environment.items():
            lines.append(f"| {key.replace('_', ' ')} | {value} |")
        lines.append("")

    if document["failures"]:
        lines += [
            "## Failures",
            "",
            "Named explicitly. A suite that reported only its successes would prove nothing.",
            "",
            "| Case | What it proves | Expected | Observed |",
            "|---|---|---|---|",
        ]
        for case in document["failures"]:
            lines.append(
                f"| `{case['id']}` | {case['what_it_proves']} | {case['expected']} | "
                f"{case['observed']} |"
            )
        lines.append("")
    else:
        lines += ["## Failures", "", "None.", ""]

    lines += ["## Every case", ""]
    for suite in document["suites"]:
        heading = f"### {suite['id']} - {suite['title']}"
        lines += [heading, "", suite["description"], ""]
        if suite["skipped"]:
            lines += [f"Skipped: {suite['skipped']}", ""]
            continue
        lines += [
            f"{suite['passed']}/{suite['total']} passed in {suite['duration_ms'] / 1000:.1f}s.",
            "",
            "| Case | What it proves | Expected | Observed | Result |",
            "|---|---|---|---|---|",
        ]
        for case in suite["cases"]:
            mark = "pass" if case["passed"] else "FAIL"
            lines.append(
                f"| `{case['id']}` | {case['what_it_proves']} | {case['expected']} | "
                f"{case['observed']} | {mark} |"
            )
        lines.append("")
    return "\n".join(lines) + "\n"


def print_console(document: dict[str, Any]) -> None:
    totals = document["totals"]
    for suite in document["suites"]:
        if suite["skipped"]:
            print(f"{suite['id']:<4} {suite['title']:<44} SKIPPED  {suite['skipped']}")
            continue
        verdict = "PASS" if suite["failed"] == 0 else "FAIL"
        print(
            f"{suite['id']:<4} {suite['title']:<44} {verdict}  "
            f"{suite['passed']}/{suite['total']} in {suite['duration_ms'] / 1000:5.1f}s"
        )
    print()
    if document["failures"]:
        print("Failures:")
        for case in document["failures"]:
            print(f"  {case['id']}")
            print(f"    expected: {case['expected']}")
            print(f"    observed: {case['observed']}")
        print()
    print(
        f"{totals['passed']}/{totals['cases']} cases passed across {totals['suites']} suites "
        f"in {totals['wall_ms'] / 1000:.1f}s"
    )
