import type { Metadata } from "next";
import Link from "next/link";

import { PageHeader, BackendDown, Card, Cell, Empty, Pill, Row, Stat, Table } from "@/components/ui";
import { backendReachable, backendRead } from "@/lib/backend";
import { relative, shorten } from "@/lib/format";
import type { ChainReport, EvidenceSummary } from "@/lib/types";

export const dynamic = "force-dynamic";
export const metadata: Metadata = { title: "Evidence" };

const OUTCOME_TONE: Record<string, "allow" | "deny" | "escalate" | "neutral"> = {
  completed: "allow",
  awaiting_payment: "escalate",
  escalated: "escalate",
  compensated: "escalate",
  compensation_failed: "deny",
  refused_verification: "deny",
  refused_kernel: "deny",
  refused_semantic: "deny",
  refused_escalation: "deny",
  refused_revoked: "deny",
  payment_failed: "deny",
};

export default async function EvidencePage() {
  if (!(await backendReachable())) return <BackendDown />;

  const { chain, packets } = await backendRead<{ chain: ChainReport; packets: EvidenceSummary[] }>(
    "/merchant/evidence?limit=100",
    { chain: { packets: 0, valid: true, problems: [] }, packets: [] },
  );

  return (
    <>
      <PageHeader
        title="Evidence"
        description="Every transaction, written once and never mutated. The packets are hash chained, so any retroactive edit is detectable, and the same chain verifies offline with the application stopped."
      />

      <div className="grid gap-3 sm:grid-cols-3">
        <Stat label="Packets in the chain" value={chain.packets} />
        <Stat
          label="Chain verification"
          value={chain.valid ? "intact" : "BROKEN"}
          tone={chain.valid ? "allow" : "deny"}
          hint={chain.valid ? "every hash link and signature checks out" : "see the problems below"}
        />
        <Stat label="Problems found" value={chain.problems.length} tone={chain.problems.length ? "deny" : "allow"} />
      </div>

      {!chain.valid && (
        <Card title="Chain problems">
          <Table head={["Sequence", "Problem"]}>
            {chain.problems.map((problem, index) => (
              <Row key={`${problem.seq}-${index}`}>
                <Cell mono>{problem.seq}</Cell>
                <Cell>
                  <span className="text-deny">{problem.problem}</span>
                </Cell>
              </Row>
            ))}
          </Table>
        </Card>
      )}

      <Card
        title="Evidence packets"
        description="Written once and never mutated. Packets are hash chained, so any retroactive edit or deletion is detectable, and the same chain can be verified offline with the application stopped."
        actions={
          <span className="text-xs text-muted">
            Offline check: <code>python tools/verify_evidence.py --jsonl reports/evidence.jsonl --jwks reports/merchant_jwks.json</code>
          </span>
        }
      >
        {packets.length === 0 ? (
          <Empty>No transaction has been recorded yet.</Empty>
        ) : (
          <Table head={["Seq", "Outcome", "Agent", "Correlation", "Recorded"]}>
            {packets.map((packet) => (
              <Row key={packet.packet_id}>
                <Cell mono>{packet.seq}</Cell>
                <Cell>
                  <Pill tone={OUTCOME_TONE[packet.outcome ?? ""] ?? "neutral"}>
                    {packet.outcome ?? "unknown"}
                  </Pill>
                </Cell>
                <Cell mono>{packet.agent_id ?? "-"}</Cell>
                <Cell>
                  <Link
                    href={`/merchant/evidence/${packet.correlation_id}`}
                    className="font-mono text-xs text-accent hover:underline"
                  >
                    {shorten(packet.correlation_id, 14, 6)}
                  </Link>
                </Cell>
                <Cell>{relative(packet.created_at)}</Cell>
              </Row>
            ))}
          </Table>
        )}
      </Card>
    </>
  );
}
