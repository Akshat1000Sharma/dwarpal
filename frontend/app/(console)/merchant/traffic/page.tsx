import type { Metadata } from "next";
import { PageHeader, BackendDown, Card, Cell, DecisionBadge, Empty, Meter, Pill, ReasonCode, Row, Table } from "@/components/ui";
import { backendReachable, backendRead } from "@/lib/backend";
import { relative } from "@/lib/format";
import type { TrafficRow } from "@/lib/types";

export const dynamic = "force-dynamic";
export const metadata: Metadata = { title: "Agent traffic" };

export default async function TrafficPage() {
  if (!(await backendReachable())) return <BackendDown />;

  const { agents } = await backendRead<{ agents: TrafficRow[] }>("/merchant/traffic", {
    agents: [],
  });

  return (
    <>
      <PageHeader
        title="Agent traffic"
        description="Which agents are transacting, under whose authority, and what they are spending against the budget the human granted."
      />

    <Card
      title="Live agent traffic"
    >
      {agents.length === 0 ? (
        <Empty>No agent has transacted yet.</Empty>
      ) : (
        <Table
          head={["Agent", "Authority", "Tier", "Window spend", "Budget", "Last decision"]}
        >
          {agents.map((agent) => (
            <Row key={agent.agent_id}>
              <Cell>
                <div className="font-mono text-xs">{agent.agent_id}</div>
                {agent.kill_switch && (
                  <div className="mt-1">
                    <Pill tone="deny">kill switch on</Pill>
                  </div>
                )}
              </Cell>
              <Cell mono>{agent.issuer_id}</Cell>
              <Cell>
                <Pill tone={agent.tier === "unverified" ? "escalate" : "allow"}>{agent.tier}</Pill>
              </Cell>
              <Cell>
                <div className="tabular-nums">{agent.window_spend.display}</div>
                <div className="text-xs text-muted">
                  {agent.window_transactions} in {Math.round(agent.window_seconds / 60)}m
                </div>
              </Cell>
              <Cell>
                <Meter
                  used={agent.budget_used.amount}
                  total={agent.budget_total.amount}
                  label={`${agent.budget_remaining.display} of ${agent.budget_total.display} left`}
                />
              </Cell>
              <Cell>
                {agent.last_verdict ? (
                  <div className="space-y-1">
                    <DecisionBadge decision={agent.last_verdict.decision} />
                    <div>
                      <ReasonCode code={agent.last_verdict.reason_code} />
                    </div>
                    <div className="text-xs text-muted">{relative(agent.last_verdict.at)}</div>
                  </div>
                ) : (
                  <span className="text-muted">-</span>
                )}
              </Cell>
            </Row>
          ))}
        </Table>
      )}
    </Card>
    </>
  );
}
