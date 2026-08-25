import type { Metadata } from "next";
import Link from "next/link";

import { PageHeader, BackendDown, Card, Cell, Empty, Pill, Row, Stat, Table } from "@/components/ui";
import { backendReachable, backendRead } from "@/lib/backend";
import { relative, shorten } from "@/lib/format";
import type { DisputeSummary } from "@/lib/types";

export const dynamic = "force-dynamic";
export const metadata: Metadata = { title: "Disputes" };

export default async function DisputesPage() {
  if (!(await backendReachable())) return <BackendDown />;

  const { disputes } = await backendRead<{ disputes: DisputeSummary[] }>("/merchant/disputes", {
    disputes: [],
  });

  const contest = disputes.filter((d) => d.recommendation === "contest").length;
  const refund = disputes.filter((d) => d.recommendation === "refund").length;

  return (
    <>
      <PageHeader
        title="Disputes"
        description="Each representment states what authority was presented, what the human constrained, and what the buyer acknowledged. Where the evidence is weak it says to refund rather than contest."
      />

      <div className="grid gap-3 sm:grid-cols-3">
        <Stat label="Disputes" value={disputes.length} />
        <Stat label="Recommended to contest" value={contest} tone="allow" />
        <Stat
          label="Recommended to refund"
          value={refund}
          tone="escalate"
          hint="knowing which not to fight is the point"
        />
      </div>

      <Card
        title="Dispute workspace"
      >
        {disputes.length === 0 ? (
          <Empty>
            No dispute has been raised. Open one from any transaction in the evidence browser.
          </Empty>
        ) : (
          <Table
            head={["Claim", "Correlation", "Evidence strength", "Recommendation", "Outcome", "Raised"]}
          >
            {disputes.map((dispute) => (
              <Row key={dispute.id}>
                <Cell>
                  <Link href={`/merchant/disputes/${dispute.id}`} className="text-accent hover:underline">
                    {dispute.claim}
                  </Link>
                </Cell>
                <Cell>
                  <Link
                    href={`/merchant/evidence/${dispute.correlation_id}`}
                    className="font-mono text-xs text-accent hover:underline"
                  >
                    {shorten(dispute.correlation_id, 12, 6)}
                  </Link>
                </Cell>
                <Cell>
                  <span className="tabular-nums">{dispute.strength_score ?? 0}</span>
                  <span className="text-xs text-muted"> / 100</span>
                </Cell>
                <Cell>
                  <Pill tone={dispute.recommendation === "contest" ? "allow" : "escalate"}>
                    {dispute.recommendation ?? "-"}
                  </Pill>
                </Cell>
                <Cell>
                  <Pill tone={dispute.outcome === "open" ? "neutral" : "allow"}>
                    {dispute.outcome}
                  </Pill>
                </Cell>
                <Cell>{relative(dispute.claimed_at)}</Cell>
              </Row>
            ))}
          </Table>
        )}
      </Card>
    </>
  );
}
