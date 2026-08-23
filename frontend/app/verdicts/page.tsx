import Link from "next/link";

import {
  BackendDown,
  Card,
  Cell,
  DecisionBadge,
  Empty,
  Json,
  Pill,
  ReasonCode,
  Row,
  Table,
} from "@/components/ui";
import { backendReachable, backendRead } from "@/lib/backend";
import { humanise, relative, timestamp } from "@/lib/format";
import type { Verdict } from "@/lib/types";

export const dynamic = "force-dynamic";

const DECISIONS = ["allow", "deny", "escalate", "challenge"] as const;

export default async function VerdictsPage({
  searchParams,
}: {
  searchParams: Promise<{ decision?: string; agent_id?: string; reason_code?: string }>;
}) {
  if (!(await backendReachable())) return <BackendDown />;

  const filters = await searchParams;
  const query = new URLSearchParams({ limit: "150" });
  if (filters.decision) query.set("decision", filters.decision);
  if (filters.agent_id) query.set("agent_id", filters.agent_id);
  if (filters.reason_code) query.set("reason_code", filters.reason_code);

  const { verdicts, total } = await backendRead<{ verdicts: Verdict[]; total: number }>(
    `/merchant/verdicts?${query.toString()}`,
    { verdicts: [], total: 0 },
  );

  const href = (decision?: string) => {
    const next = new URLSearchParams();
    if (decision) next.set("decision", decision);
    if (filters.agent_id) next.set("agent_id", filters.agent_id);
    const suffix = next.toString();
    return suffix ? `/verdicts?${suffix}` : "/verdicts";
  };

  return (
    <Card
      title="Verdict log"
      description="Every policy decision with its reason code and the evidence it was decided on. Refusals are shown as prominently as approvals."
      actions={
        <div className="flex flex-wrap gap-1">
          <Link
            href={href()}
            className={`rounded px-2 py-1 text-xs ${
              !filters.decision ? "bg-surface-muted font-medium" : "text-muted hover:text-foreground"
            }`}
          >
            All
          </Link>
          {DECISIONS.map((decision) => (
            <Link
              key={decision}
              href={href(decision)}
              className={`rounded px-2 py-1 text-xs capitalize ${
                filters.decision === decision
                  ? "bg-surface-muted font-medium"
                  : "text-muted hover:text-foreground"
              }`}
            >
              {decision}
            </Link>
          ))}
        </div>
      }
    >
      {verdicts.length === 0 ? (
        <Empty>No verdicts match this filter. {total} decisions recorded in total.</Empty>
      ) : (
        <Table head={["When", "Agent", "Action", "Decision", "Reason code", "Agent should", "Amount", "Evidence"]}>
          {verdicts.map((verdict) => (
            <Row key={verdict.id}>
              <Cell>
                <div>{relative(verdict.created_at)}</div>
                <div className="text-xs text-muted">{timestamp(verdict.created_at)}</div>
              </Cell>
              <Cell mono>
                <Link
                  href={`/verdicts?agent_id=${encodeURIComponent(verdict.agent_id)}`}
                  className="hover:underline"
                >
                  {verdict.agent_id}
                </Link>
              </Cell>
              <Cell>{verdict.action}</Cell>
              <Cell>
                <DecisionBadge decision={verdict.decision} />
              </Cell>
              <Cell>
                <ReasonCode code={verdict.reason_code} />
              </Cell>
              <Cell>
                <Pill>{humanise(verdict.agent_action)}</Pill>
              </Cell>
              <Cell>{verdict.amount.display}</Cell>
              <Cell>
                <details>
                  <summary className="cursor-pointer text-xs text-accent">
                    {verdict.correlation_id ? "inspect" : "-"}
                  </summary>
                  <div className="mt-2 max-w-xl space-y-2">
                    {verdict.correlation_id && (
                      <Link
                        href={`/evidence/${verdict.correlation_id}`}
                        className="text-xs text-accent hover:underline"
                      >
                        Open the evidence packet
                      </Link>
                    )}
                    <Json value={verdict.evidence} />
                  </div>
                </details>
              </Cell>
            </Row>
          ))}
        </Table>
      )}
    </Card>
  );
}
