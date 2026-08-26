import type { Metadata } from "next";
import { PageHeader, BackendDown, Card, Cell, Empty, Pill, Row, Stat, Table } from "@/components/ui";
import { backendReachable, backendRead } from "@/lib/backend";
import { percent, timestamp } from "@/lib/format";
import type { Reports } from "@/lib/types";

export const dynamic = "force-dynamic";
export const metadata: Metadata = { title: "Scorecards" };

export default async function ScorecardsPage() {
  if (!(await backendReachable())) return <BackendDown />;

  const reports = await backendRead<Reports>("/merchant/reports", {
    generated: false,
    attack_scorecard: null,
    dispute_defence: null,
  });

  if (!reports.generated) {
    return (
      <Card title="No reports have been generated yet">
        <div className="space-y-2 px-5 py-6 text-sm text-muted">
          <p>Both numbers are produced by running code, never estimated. Generate them with:</p>
          <pre className="scroll-x rounded border border-line bg-surface-muted p-3 text-xs">
            cd backend &amp;&amp; python -m app.cli reports
          </pre>
          <p>
            The reports land in <code>backend/reports/</code> and CI uploads them as build
            artifacts.
          </p>
        </div>
      </Card>
    );
  }

  const attack = reports.attack_scorecard;
  const defence = reports.dispute_defence;

  return (
    <>
      <PageHeader
        title="Scorecards"
        description="The two headline numbers, produced by running code rather than estimated. Blocks and false positives are always reported together."
      />

      {attack && (
        <>
          <Card
            title="Attack scorecard"
            description="Blocks, misses and the false-positive rate against matched legitimate traffic, always reported together. A gate that refuses everything would score perfectly on the left and be useless."
            actions={
              <span className="text-xs text-muted">generated {timestamp(attack.generated_at)}</span>
            }
          >
            <div className="grid gap-3 p-5 sm:grid-cols-2 lg:grid-cols-4">
              <Stat
                label="Attacks blocked"
                value={`${attack.adversarial.blocked} / ${attack.adversarial.total}`}
                tone="allow"
                hint={`${percent(attack.adversarial.block_rate)} across ${attack.adversarial.techniques} techniques`}
              />
              <Stat
                label="Missed"
                value={attack.adversarial.missed}
                tone={attack.adversarial.missed ? "deny" : "allow"}
                hint="named individually below"
              />
              <Stat
                label="Legitimate traffic allowed"
                value={`${attack.benign.allowed} / ${attack.benign.total}`}
                hint={`${attack.benign.escalated_to_human} escalated to the human by design`}
              />
              <Stat
                label="False positives"
                value={attack.benign.false_positives}
                tone={attack.benign.false_positives ? "deny" : "allow"}
                hint={percent(attack.benign.false_positive_rate)}
              />
            </div>
          </Card>

          <Card title="Misses">
            {attack.misses.length === 0 ? (
              <Empty>
                None. Every adversarial scenario was blocked with a reason code it declared in
                advance.
              </Empty>
            ) : (
              <Table head={["Scenario", "Family", "Expected", "Observed"]}>
                {attack.misses.map((miss, index) => (
                  <Row key={`${String(miss.id)}-${index}`}>
                    <Cell mono>{String(miss.id)}</Cell>
                    <Cell>{String(miss.family)}</Cell>
                    <Cell>{(miss.expected_reason_codes as string[])?.join(", ") || "blocked"}</Cell>
                    <Cell>
                      <span className="text-deny">{String(miss.observed_reason_code)}</span>
                    </Cell>
                  </Row>
                ))}
              </Table>
            )}
          </Card>

          <Card title="Settled without asking">
            {(attack.settled_without_asking_detail ?? []).length === 0 ? (
              <Empty>
                None. Every scenario that declared it needed a human reached one. This is the
                mirror of a false positive and the more serious direction to fail in, so it is
                counted here rather than folded into the allowed total.
              </Empty>
            ) : (
              <Table head={["Scenario", "Completed as"]}>
                {attack.settled_without_asking_detail.map((entry, index) => (
                  <Row key={`${String(entry.id)}-${index}`}>
                    <Cell mono>{String(entry.id)}</Cell>
                    <Cell>
                      <span className="text-deny">{String(entry.observed_reason_code)}</span>
                    </Cell>
                  </Row>
                ))}
              </Table>
            )}
          </Card>

          <Card
            title="Every technique, and how many cases it ran as"
            description={`A technique is one attack idea; a case is that idea executed against one item, issuing tier and amount. Families covered: ${attack.families.join(", ")}. The per-case table for all ${attack.results_count} cases is in the generated artifact under backend/reports/.`}
          >
            <Table head={["Technique", "Family", "Cases", "Blocked", "Result"]}>
              {attack.by_technique.map((entry) => (
                <Row key={entry.technique}>
                  <Cell mono>{entry.technique}</Cell>
                  <Cell>{entry.family}</Cell>
                  <Cell>{entry.cases}</Cell>
                  <Cell>{entry.blocked}</Cell>
                  <Cell>
                    <Pill tone={entry.missed ? "deny" : "allow"}>
                      {entry.missed ? `${entry.missed} MISSED` : "as declared"}
                    </Pill>
                  </Cell>
                </Row>
              ))}
            </Table>
          </Card>
        </>
      )}

      {defence && (
        <>
          <Card
            title="Dispute defence rate"
            description="Each synthetic dispute is scored twice: against the evidence packet Dwarpal filed, and against a baseline merchant that kept only the payment record."
            actions={
              <span className="text-xs text-muted">
                generated {timestamp(defence.generated_at)}
              </span>
            }
          >
            <div className="grid gap-3 p-5 sm:grid-cols-2 lg:grid-cols-4">
              <Stat
                label="Defensible with evidence"
                value={`${defence.with_evidence.defensible} / ${defence.total}`}
                tone="allow"
                hint={percent(defence.with_evidence.defence_rate)}
              />
              <Stat
                label="Defensible without"
                value={`${defence.baseline.defensible} / ${defence.total}`}
                tone="deny"
                hint={percent(defence.baseline.defence_rate)}
              />
              <Stat label="Improvement" value={percent(defence.improvement)} tone="allow" />
              <Stat
                label="Mean evidence strength"
                value={defence.with_evidence.mean_strength.toFixed(1)}
                hint={`baseline ${defence.baseline.mean_strength.toFixed(1)}`}
              />
            </div>
          </Card>

          <Card
            title="Where the responder declines to fight"
            description="A responder that recommends contesting everything is worthless, so these are reported rather than hidden."
          >
            {defence.refund_recommended.length === 0 ? (
              <Empty>None in this batch.</Empty>
            ) : (
              <Table head={["Case", "Score", "Why"]}>
                {defence.refund_recommended.map((entry) => (
                  <Row key={entry.case_id}>
                    <Cell mono>{entry.case_id}</Cell>
                    <Cell>{entry.strength_score}</Cell>
                    <Cell>
                      <span className="text-xs text-muted">
                        {entry.weaknesses[0] ?? "insufficient evidence"}
                      </span>
                    </Cell>
                  </Row>
                ))}
              </Table>
            )}
          </Card>

          <Card
            title="Where the per-dispute rows are"
            description={`${defence.disputes_count} disputes were scored. Every one of them, with its score and recommendation, is written to backend/reports/ by "python -m app.cli reports", and CI uploads that directory as an artifact on every run.`}
          >
            <Empty>
              Not shown here on purpose: sending every row to this page would put most of a
              megabyte in front of four figures. The reports directory is generated rather than
              committed, so run the command above to produce it locally.
            </Empty>
          </Card>
        </>
      )}
    </>
  );
}
