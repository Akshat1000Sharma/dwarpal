import type { Metadata } from "next";
import Link from "next/link";

import { resolveException } from "@/app/actions";
import { ResolveException } from "@/components/controls";
import { PageHeader, BackendDown, Card, Cell, DecisionBadge, Empty, ReasonCode, Row, Stat, Table } from "@/components/ui";
import { backendReachable, backendRead } from "@/lib/backend";
import { relative } from "@/lib/format";
import type { Escalation, Overview, PaymentExceptionRow, Verdict } from "@/lib/types";

export const dynamic = "force-dynamic";
export const metadata: Metadata = { title: "Overview" };

export default async function OverviewPage() {
  if (!(await backendReachable())) {
    return <BackendDown />;
  }

  const [overview, verdicts, escalations, exceptions] = await Promise.all([
    backendRead<Overview | null>("/merchant/overview", null),
    backendRead<{ verdicts: Verdict[] }>("/merchant/verdicts?limit=8", { verdicts: [] }),
    backendRead<{ escalations: Escalation[] }>("/merchant/escalations", { escalations: [] }),
    backendRead<{ exceptions: PaymentExceptionRow[] }>("/merchant/exceptions", { exceptions: [] }),
  ]);

  if (!overview) return <BackendDown />;

  const pending = escalations.escalations.filter((e) => e.status === "pending");
  const openExceptions = exceptions.exceptions.filter((e) => !e.resolved);

  return (
    <>
      <PageHeader
        title="Overview"
        description="What the gate has decided in the last day, what is waiting on a human, and where the merchant's own records disagree with Razorpay."
      />

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Stat
          label={`Approved, last ${overview.window_hours}h`}
          value={overview.verdicts.allow}
          tone="allow"
          hint={`${overview.verdicts.total} decisions in total`}
        />
        <Stat
          label="Refused"
          value={overview.verdicts.deny}
          tone="deny"
          hint="a refusal is evidence, not an error"
        />
        <Stat
          label="Escalated to a human"
          value={overview.verdicts.escalate}
          tone="escalate"
          hint="the kernel could not decide alone"
        />
        <Stat
          label="Credentials challenged"
          value={overview.verdicts.challenge}
          hint="unverified agents above the ceiling"
        />
      </div>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Stat label="Captured" value={overview.captured.display} />
        <Stat label="Refunded" value={overview.refunded.display} />
        <Stat label="Open mandates" value={overview.open_mandates} />
        <Stat label="Evidence packets" value={overview.evidence_packets} />
      </div>

      {(pending.length > 0 || openExceptions.length > 0) && (
        <div className="grid gap-3 lg:grid-cols-2">
          {pending.length > 0 && (
            <Card
              title="Escalations awaiting the human"
              description="An escalation that is never answered becomes a denial. Timeouts never fail open."
            >
              <Table head={["Constraint", "Amount", "Deadline"]}>
                {pending.map((escalation) => (
                  <Row key={escalation.escalation_id}>
                    <Cell>{escalation.constraint}</Cell>
                    <Cell>
                      {escalation.amount.currency} {(escalation.amount.amount / 100).toFixed(2)}
                    </Cell>
                    <Cell>{relative(escalation.deadline_at)}</Cell>
                  </Row>
                ))}
              </Table>
            </Card>
          )}
          {openExceptions.length > 0 && (
            <Card
              title="Payment exceptions"
              description="Razorpay is authoritative. A disagreement is recorded here rather than silently corrected."
            >
              <Table head={["Kind", "Correlation", "Seen", ""]}>
                {openExceptions.slice(0, 6).map((exception) => (
                  <Row key={exception.id}>
                    <Cell>
                      <span className="text-deny">{exception.kind}</span>
                    </Cell>
                    <Cell mono>{exception.correlation_id}</Cell>
                    <Cell>{relative(exception.created_at)}</Cell>
                    <Cell>
                      <ResolveException
                        exceptionId={exception.id}
                        action={resolveException}
                      />
                    </Cell>
                  </Row>
                ))}
              </Table>
            </Card>
          )}
        </div>
      )}

      <Card
        title="Latest policy decisions"
        description="Every money action passes through the deterministic kernel and carries a reason code from a closed set."
        actions={
          <Link href="/merchant/verdicts" className="text-xs text-accent hover:underline">
            Full verdict log
          </Link>
        }
      >
        {verdicts.verdicts.length === 0 ? (
          <Empty>
            No decisions yet. Drive one with{" "}
            <code>python interop/run_interop.py</code> from the backend directory.
          </Empty>
        ) : (
          <Table head={["When", "Agent", "Action", "Decision", "Reason", "Amount"]}>
            {verdicts.verdicts.map((verdict) => (
              <Row key={verdict.id}>
                <Cell>{relative(verdict.created_at)}</Cell>
                <Cell mono>{verdict.agent_id}</Cell>
                <Cell>{verdict.action}</Cell>
                <Cell>
                  <DecisionBadge decision={verdict.decision} />
                </Cell>
                <Cell>
                  <ReasonCode code={verdict.reason_code} />
                </Cell>
                <Cell>{verdict.amount.display}</Cell>
              </Row>
            ))}
          </Table>
        )}
      </Card>
    </>
  );
}
