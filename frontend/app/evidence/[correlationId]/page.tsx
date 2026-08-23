import Link from "next/link";
import { notFound } from "next/navigation";

import { openDispute } from "@/app/actions";
import { OpenDispute } from "@/components/controls";
import { BackendDown, Card, Cell, Json, Pill, Row, Stat, Table } from "@/components/ui";
import { backendFetch, backendReachable } from "@/lib/backend";
import { money, shorten, timestamp } from "@/lib/format";
import type { EvidencePacket } from "@/lib/types";

export const dynamic = "force-dynamic";

type Detail = {
  correlation_id: string;
  chain_valid: boolean;
  chain_problems: Array<{ seq: number; problem: string }>;
  packets: EvidencePacket[];
};

export default async function EvidenceDetailPage({
  params,
}: {
  params: Promise<{ correlationId: string }>;
}) {
  if (!(await backendReachable())) return <BackendDown />;
  const { correlationId } = await params;

  let detail: Detail;
  try {
    detail = await backendFetch<Detail>(`/merchant/evidence/${encodeURIComponent(correlationId)}`);
  } catch {
    notFound();
  }

  const latest = detail.packets[detail.packets.length - 1];
  const body = latest.body;
  const checkout = body.checkout as Record<string, unknown>;
  const verification = body.verification as Record<string, unknown>;
  const snapshot = (checkout.catalog_snapshot as Array<Record<string, unknown>>) ?? [];

  return (
    <>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="font-mono text-sm">{detail.correlation_id}</h2>
          <p className="text-xs text-muted">
            {detail.packets.length} packet(s) recorded for this transaction
          </p>
        </div>
        <Link href="/evidence" className="text-xs text-accent hover:underline">
          Back to the evidence browser
        </Link>
      </div>

      <div className="grid gap-3 sm:grid-cols-3">
        <Stat label="Outcome" value={body.outcome} />
        <Stat
          label="Chain verification"
          value={detail.chain_valid ? "intact" : "BROKEN"}
          tone={detail.chain_valid ? "allow" : "deny"}
        />
        <Stat label="AP2 schema revision" value={shorten(body.protocol.schema_revision, 8, 4)} />
      </div>

      <Card
        title="What authority was presented"
        description="The credential chain exactly as the agent put it on the wire, so a third party can re-verify every signature."
      >
        <div className="space-y-3 px-5 py-4 text-sm">
          <dl className="grid gap-3 sm:grid-cols-2">
            <div>
              <dt className="text-xs text-muted">Agent</dt>
              <dd className="font-mono text-xs">{body.agent_id}</dd>
            </div>
            <div>
              <dt className="text-xs text-muted">Issuing authority</dt>
              <dd className="font-mono text-xs">{String(verification.issuer_id ?? "-")}</dd>
            </div>
            <div>
              <dt className="text-xs text-muted">Verification steps passed</dt>
              <dd className="text-xs">
                {((verification.steps_passed as string[]) ?? []).join(", ") || "none"}
              </dd>
            </div>
            <div>
              <dt className="text-xs text-muted">Acknowledged policy hash</dt>
              <dd className="font-mono text-xs">{String(checkout.policy_hash ?? "-")}</dd>
            </div>
          </dl>
          <details>
            <summary className="cursor-pointer text-xs text-accent">
              Credential chain and constraint evaluation
            </summary>
            <div className="mt-2 space-y-2">
              <Json value={body.credential_chain} />
              <Json value={verification} />
            </div>
          </details>
        </div>
      </Card>

      {snapshot.length > 0 && (
        <Card
          title="What the buyer was shown"
          description="Prices and availability frozen at quote time. A reference to a mutable product record would not be a snapshot."
        >
          <Table head={["SKU", "Price at quote", "Available then", "Constraints"]}>
            {snapshot.map((entry, index) => (
              <Row key={`${String(entry.sku)}-${index}`}>
                <Cell mono>{String(entry.sku)}</Cell>
                <Cell>
                  {money(Number(entry.price_minor ?? 0), String(entry.currency ?? "INR"))}
                </Cell>
                <Cell>{String(entry.available_at_quote ?? "-")}</Cell>
                <Cell>
                  <Json value={entry.purchase_constraints} />
                </Cell>
              </Row>
            ))}
          </Table>
        </Card>
      )}

      <Card title="Policy decisions">
        <Table head={["Action", "Decision", "Reason code", "Amount", "When"]}>
          {body.verdicts.map((verdict, index) => (
            <Row key={String(verdict.id ?? index)}>
              <Cell>{String(verdict.action)}</Cell>
              <Cell>
                <Pill tone={verdict.decision === "allow" ? "allow" : "deny"}>
                  {String(verdict.decision)}
                </Pill>
              </Cell>
              <Cell mono>{String(verdict.reason_code)}</Cell>
              <Cell>
                {money(
                  Number((verdict.amount as { amount?: number })?.amount ?? 0),
                  String((verdict.amount as { currency?: string })?.currency ?? "INR"),
                )}
              </Cell>
              <Cell>{timestamp(String(verdict.created_at))}</Cell>
            </Row>
          ))}
        </Table>
      </Card>

      {body.escalations.length > 0 && (
        <Card
          title="Escalations"
          description="Every escalation, why it was raised, the answer and the timing. An unanswered escalation is a denial."
        >
          <Table head={["Constraint", "Status", "Raised", "Deadline", "Answered"]}>
            {body.escalations.map((escalation) => (
              <Row key={escalation.escalation_id}>
                <Cell>{escalation.constraint}</Cell>
                <Cell>
                  <Pill tone={escalation.status === "approved" ? "allow" : "deny"}>
                    {escalation.status}
                  </Pill>
                </Cell>
                <Cell>{timestamp(escalation.created_at)}</Cell>
                <Cell>{timestamp(escalation.deadline_at)}</Cell>
                <Cell>{timestamp(escalation.answered_at)}</Cell>
              </Row>
            ))}
          </Table>
        </Card>
      )}

      {body.semantic_checks.length > 0 && (
        <Card
          title="Semantic checks"
          description="The model is consulted only on constraints the kernel could not evaluate, and can only deny or escalate."
        >
          <div className="px-5 py-4">
            <Json value={body.semantic_checks} />
          </div>
        </Card>
      )}

      {(body.payments.length > 0 || body.refunds.length > 0) && (
        <Card title="Money">
          <div className="space-y-3 px-5 py-4">
            {body.payments.length > 0 && <Json value={body.payments} />}
            {body.refunds.length > 0 && <Json value={body.refunds} />}
          </div>
        </Card>
      )}

      {body.timings.length > 0 && (
        <Card title="Timing for each step">
          <Table head={["Step", "Started", "Duration"]}>
            {body.timings.map((step, index) => (
              <Row key={`${step.step}-${index}`}>
                <Cell>{step.step}</Cell>
                <Cell>{timestamp(step.started_at)}</Cell>
                <Cell>{step.duration_ms === null ? "-" : `${step.duration_ms} ms`}</Cell>
              </Row>
            ))}
          </Table>
        </Card>
      )}

      <Card
        title="Chain position"
        description="Each entry commits to its predecessor, so any retroactive edit breaks every later link."
      >
        <Table head={["Seq", "Previous hash", "Entry hash", "Recorded"]}>
          {detail.packets.map((packet) => (
            <Row key={packet.packet_id}>
              <Cell mono>{packet.seq}</Cell>
              <Cell mono>{shorten(packet.prev_hash, 12, 6)}</Cell>
              <Cell mono>{shorten(packet.entry_hash, 12, 6)}</Cell>
              <Cell>{timestamp(packet.created_at)}</Cell>
            </Row>
          ))}
        </Table>
      </Card>

      <Card
        title="Dispute this transaction"
        description="Assembles the representment from this packet and states whether the evidence is strong enough to contest."
      >
        <div className="px-5 py-4">
          <OpenDispute correlationId={detail.correlation_id} action={openDispute} />
        </div>
      </Card>
    </>
  );
}
