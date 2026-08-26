"""Report generation.

Two numbers, both produced by running code rather than estimated:

    - the attack scorecard: how many adversarial scenarios were blocked, which were not, and the
      false-positive rate against the benign corpus.
    - the dispute defence rate: across a batch of synthetic disputes, how many the evidence packet
      supports successfully compared with a baseline that has no evidence packet.

A scorecard that reported only successes would be read as dishonest, so misses are named
explicitly in both the JSON and the Markdown.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.db.base import utcnow
from app.harness.runner import CorpusReport
from app.settings import settings


def reports_directory() -> Path:
    directory = settings.resolve("./reports")
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


# One family the corpus cannot host: a genuine concurrent draw needs several committed sessions
# racing, and the corpus runs inside a single transaction. It is named here so the headline number
# is not read as covering it.
ENFORCED_ELSEWHERE = [
    "",
    "## Enforced outside this corpus",
    "",
    "- Concurrent draw against a single mandate cap, in `tests/test_concurrency.py`. That suite "
    "races many committed sessions against one cap and includes a naive check-then-write control "
    "that demonstrably breaches it, which a single-transaction corpus cannot reproduce.",
    "",
    "## What these numbers do not claim",
    "",
    "Stated here rather than left for a reader to infer.",
    "",
    "- **The semantic check runs against a deterministic offline classifier, not a model.** The "
    "corpus has to be reproducible and cannot depend on a network call, so the prompt-injection "
    "cases prove that the gate around the model holds: untrusted product text and agent free text "
    "reach the classifier inside a delimited block, an unexpected reply is treated as an "
    "escalation, and a `violates` reply denies. They do not measure how a particular model "
    "behaves when it reads an injection payload.",
    "- **Stateful defences are measured within a case, not across the run.** Inventory, runtime "
    "keys and spend history are reset between cases so that one case cannot decide another. "
    "Techniques that need history, such as structuring and recurrence, build it inside their own "
    "case using repeated attempts.",
    "- **A multi-attempt technique is scored on its final attempt.** That is the attempt the "
    "technique is about; the earlier ones exist to create the state it needs.",
]


def render_attack_markdown(report: dict[str, Any]) -> str:
    adversarial = report["adversarial"]
    benign = report["benign"]
    techniques = adversarial.get("techniques", len(report.get("techniques", [])))
    lines = [
        "# Dwarpal attack scorecard",
        "",
        f"Generated {report['generated_at']} for merchant `{report['merchant']}`.",
        "",
        f"**{adversarial['total']} adversarial cases across {techniques} techniques in "
        f"{len(report['families'])} families**, and {benign['total']} cases of matched legitimate",
        "traffic. A technique is one attack idea; a case is that idea executed against one item,",
        "issuing tier and amount. They are counted separately so neither number can be read as the",
        "other.",
        "",
        "Both halves are reported together on purpose. A gate that refuses all traffic would",
        "show a perfect block rate and be useless, so the false-positive rate against matched",
        "legitimate traffic is given equal weight.",
        "",
        "## Headline",
        "",
        "| Measure | Value |",
        "|---|---|",
        f"| Adversarial techniques | {techniques} |",
        f"| Adversarial cases executed | {adversarial['total']} |",
        f"| Blocked | {adversarial['blocked']} |",
        f"| Blocked with the expected reason code | {adversarial['passed']} |",
        f"| **Missed** | **{adversarial['missed']}** |",
        f"| Block rate | {_pct(adversarial['block_rate'])} |",
        f"| Benign cases executed | {benign['total']} |",
        f"| Allowed | {benign['allowed']} |",
        f"| Escalated to the human by design | {benign['escalated_to_human']} |",
        f"| **False positives** | **{benign['false_positives']}** |",
        f"| **Settled without asking** | **{benign.get('settled_without_asking', 0)}** |",
        f"| False-positive rate | {_pct(benign['false_positive_rate'])} |",
        "",
        "## Families covered",
        "",
    ]
    lines.extend(f"- {family}" for family in report["families"])
    if report.get("by_technique"):
        lines.extend(
            [
                "",
                "## Every technique, and how many cases it was executed as",
                "",
                "| Technique | Family | Cases | Blocked | Missed |",
                "|---|---|---|---|---|",
            ]
        )
        for entry in report["by_technique"]:
            lines.append(
                f"| `{entry['technique']}` | {entry['family']} | {entry['cases']} | "
                f"{entry['blocked']} | {entry['missed']} |"
            )
    lines.extend(ENFORCED_ELSEWHERE)
    lines.extend(["", "## Misses", ""])
    if report["misses"]:
        lines.append("| Scenario | Family | Expected | Observed |")
        lines.append("|---|---|---|---|")
        for miss in report["misses"]:
            expected = ", ".join(miss["expected_reason_codes"]) or "blocked"
            lines.append(
                f"| `{miss['id']}` | {miss['family']} | {expected} | "
                f"{miss['observed_reason_code']} ({miss['observed_status']}) |"
            )
    else:
        lines.append("None. Every adversarial scenario was blocked with a reason code it declared.")

    lines.extend(
        [
            "",
            "## False positives",
            "",
            "Legitimate traffic refused contrary to what the scenario declared. Traffic the",
            "merchant deliberately escalated to the human is counted separately above, because",
            "asking rather than guessing is the designed behaviour, not an error.",
            "",
        ]
    )
    if report["false_positive_detail"]:
        lines.append("| Scenario | Refused with |")
        lines.append("|---|---|")
        for entry in report["false_positive_detail"]:
            lines.append(f"| `{entry['id']}` | {entry['observed_reason_code']} |")
    else:
        lines.append("None. Every legitimate scenario was handled as declared.")

    lines.extend(
        [
            "",
            "## Settled without asking",
            "",
            "Legitimate traffic the scenario declared should reach a human, which the merchant",
            "completed on its own instead. This is the mirror of a false positive and the more",
            "serious direction to fail in, so it is counted rather than folded into the",
            "allowed total.",
            "",
        ]
    )
    if report.get("settled_without_asking_detail"):
        lines.append("| Scenario | Completed as |")
        lines.append("|---|---|")
        for entry in report["settled_without_asking_detail"]:
            lines.append(f"| `{entry['id']}` | {entry['observed_reason_code']} |")
    else:
        lines.append("None. Every scenario that declared it needed a human reached one.")

    lines.extend(
        [
            "",
            "## Every scenario",
            "",
            "| Scenario | Kind | Blocked | Reason code | Pass |",
            "|---|---|---|---|---|",
        ]
    )
    for entry in report["results"]:
        lines.append(
            f"| `{entry['id']}` | {entry['kind']} | {entry['observed_blocked']} | "
            f"{entry['observed_reason_code']} | {'yes' if entry['passed'] else 'NO'} |"
        )
    return "\n".join(lines) + "\n"


def render_dispute_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Dwarpal dispute defence rate",
        "",
        f"Generated {report['generated_at']}.",
        "",
        "Each synthetic dispute is scored twice: once against the evidence packet Dwarpal filed,",
        "and once against a baseline merchant that kept only the payment record. The difference is",
        "what the Evidence Locker is worth.",
        "",
        "One thing to read the case count correctly. A representment is scored on the evidence the",
        "merchant holds, not on what the cardholder alleged, so the claim texts are carried",
        "into the representment and do not move the score. The batch varies the item, the issuing",
        "tier, the buyer region and the transaction variant, and it is those that change the",
        "outcome; the claims vary the wording of the same argument.",
        "",
        "## Headline",
        "",
        "| Measure | With evidence | Baseline, no evidence |",
        "|---|---|---|",
        f"| Disputes | {report['total']} | {report['total']} |",
        f"| Defensible | {report['with_evidence']['defensible']} | "
        f"{report['baseline']['defensible']} |",
        f"| Defence rate | {_pct(report['with_evidence']['defence_rate'])} | "
        f"{_pct(report['baseline']['defence_rate'])} |",
        f"| Mean evidence strength | {report['with_evidence']['mean_strength']:.1f} | "
        f"{report['baseline']['mean_strength']:.1f} |",
        "",
        f"Improvement: {_pct(report['improvement'])} of the batch moves from unwinnable to",
        "defensible once the evidence packet exists.",
        "",
        "## Refund recommendations",
        "",
        "Knowing which disputes not to fight is the judgement being demonstrated. These are the",
        "cases where Dwarpal holds evidence and still recommends refunding.",
        "",
    ]
    if report["refund_recommended"]:
        lines.append("| Correlation | Score | Why |")
        lines.append("|---|---|---|")
        for entry in report["refund_recommended"]:
            reason = entry["weaknesses"][0] if entry["weaknesses"] else "insufficient evidence"
            lines.append(f"| `{entry['correlation_id']}` | {entry['strength_score']} | {reason} |")
    else:
        lines.append("None in this batch.")

    lines.extend(
        [
            "",
            "## Every dispute",
            "",
            "| Correlation | Outcome under test | Score | Recommendation |",
            "|---|---|---|---|",
        ]
    )
    for entry in report["disputes"]:
        lines.append(
            f"| `{entry['correlation_id']}` | {entry['transaction_outcome']} | "
            f"{entry['strength_score']} | {entry['recommendation']} |"
        )
    return "\n".join(lines) + "\n"


def write_attack_scorecard(report: CorpusReport) -> dict[str, Path]:
    directory = reports_directory()
    document = report.as_dict()
    json_path = directory / "attack_scorecard.json"
    md_path = directory / "attack_scorecard.md"
    json_path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    md_path.write_text(render_attack_markdown(document), encoding="utf-8")
    return {"json": json_path, "markdown": md_path}


def write_dispute_report(document: dict[str, Any]) -> dict[str, Path]:
    directory = reports_directory()
    json_path = directory / "dispute_defence.json"
    md_path = directory / "dispute_defence.md"
    json_path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    md_path.write_text(render_dispute_markdown(document), encoding="utf-8")
    return {"json": json_path, "markdown": md_path}


def empty_dispute_document() -> dict[str, Any]:
    return {
        "generated_at": utcnow().isoformat(),
        "total": 0,
        "with_evidence": {"defensible": 0, "defence_rate": 0.0, "mean_strength": 0.0},
        "baseline": {"defensible": 0, "defence_rate": 0.0, "mean_strength": 0.0},
        "improvement": 0.0,
        "refund_recommended": [],
        "disputes": [],
    }
