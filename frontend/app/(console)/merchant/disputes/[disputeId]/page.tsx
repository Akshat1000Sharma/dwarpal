import Link from "next/link";
import { notFound } from "next/navigation";

import { decideDispute } from "@/app/actions";
import { DisputeDecision } from "@/components/controls";
import { BackendDown, Card, Cell, Pill, Row, Stat, Table } from "@/components/ui";
import { backendFetch, backendReachable } from "@/lib/backend";
import { timestamp } from "@/lib/format";
import type { DisputeDetail } from "@/lib/types";

export const dynamic = "force-dynamic";

export default async function DisputeDetailPage({
  params,
}: {
  params: Promise<{ disputeId: string }>;
}) {
  if (!(await backendReachable())) return <BackendDown />;
  const { disputeId } = await params;

  let dispute: DisputeDetail;
  try {
    dispute = await backendFetch<DisputeDetail>(`/merchant/disputes/${disputeId}`);
  } catch {
    notFound();
  }

  const representment = dispute.representment;
  const contest = representment.recommendation === "contest";

  return (
    <>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold">{dispute.claim}</h2>
          <p className="text-xs text-muted">
            Raised {timestamp(dispute.claimed_at)} against{" "}
            <Link
              href={`/merchant/evidence/${dispute.correlation_id}`}
              className="font-mono text-accent hover:underline"
            >
              {dispute.correlation_id}
            </Link>
          </p>
        </div>
        <Link href="/merchant/disputes" className="text-xs text-accent hover:underline">
          Back to disputes
        </Link>
      </div>

      <div className="grid gap-3 sm:grid-cols-3">
        <Stat
          label="Recommendation"
          value={representment.recommendation}
          tone={contest ? "allow" : "escalate"}
        />
        <Stat
          label="Evidence strength"
          value={`${representment.strength_score} / 100`}
          tone={contest ? "allow" : "escalate"}
          hint={`contest threshold is ${representment.contest_threshold}`}
        />
        <Stat label="Recorded outcome" value={dispute.outcome} />
      </div>

      {!contest && (
        <Card title="Why this should be refunded rather than contested">
          <ul className="list-disc space-y-1 px-8 py-4 text-sm text-muted">
            {representment.weaknesses.map((weakness) => (
              <li key={weakness}>{weakness}</li>
            ))}
          </ul>
        </Card>
      )}

      <Card
        title="Representment"
        description="What the merchant would put in front of the network."
      >
        <div className="space-y-3 px-5 py-4 text-sm">
          {representment.narrative.map((line) => (
            <p key={line}>{line}</p>
          ))}
        </div>
      </Card>

      <Card
        title="Evidence scoring"
        description="The reasoning is encoded rather than asserted, so the recommendation can be audited."
      >
        <Table head={["Check", "Present", "Weight", "Awarded", "Why it matters"]}>
          {representment.factors.map((factor) => (
            <Row key={factor.key}>
              <Cell>{factor.description}</Cell>
              <Cell>
                <Pill tone={factor.present ? "allow" : "deny"}>{factor.present ? "yes" : "no"}</Pill>
              </Cell>
              <Cell>{factor.weight}</Cell>
              <Cell>
                <span className="tabular-nums">{factor.awarded}</span>
              </Cell>
              <Cell>
                <span className="text-xs text-muted">{factor.detail}</span>
              </Cell>
            </Row>
          ))}
        </Table>
      </Card>

      {representment.timeline.length > 0 && (
        <Card title="What happened, and when">
          <Table head={["When", "Event"]}>
            {representment.timeline.map((event, index) => (
              <Row key={`${event.at}-${index}`}>
                <Cell>{timestamp(event.at)}</Cell>
                <Cell>{event.event}</Cell>
              </Row>
            ))}
          </Table>
        </Card>
      )}

      {contest && representment.weaknesses.length > 0 && (
        <Card title="Known weaknesses in this defence">
          <ul className="list-disc space-y-1 px-8 py-4 text-sm text-muted">
            {representment.weaknesses.map((weakness) => (
              <li key={weakness}>{weakness}</li>
            ))}
          </ul>
        </Card>
      )}

      <Card title="Record the decision">
        <div className="px-5 py-4">
          <DisputeDecision disputeId={dispute.id} action={decideDispute} />
        </div>
      </Card>
    </>
  );
}
