import type { Metadata } from "next";
import { setKillSwitch, updateAgentLimits } from "@/app/actions";
import { AgentLimits, CategoryGate, KillSwitch } from "@/components/controls";
import { PageHeader, BackendDown, Card, Cell, Empty, Pill, Row, Table } from "@/components/ui";
import { backendReachable, backendRead } from "@/lib/backend";
import type { Agent } from "@/lib/types";

export const dynamic = "force-dynamic";
export const metadata: Metadata = { title: "Agent controls" };

export default async function AgentsPage() {
  if (!(await backendReachable())) return <BackendDown />;

  const [{ agents }, categories] = await Promise.all([
    backendRead<{ agents: Agent[] }>("/merchant/agents", { agents: [] }),
    backendRead<{ categories: string[] }>("/catalog/categories", { categories: [] }),
  ]);

  return (
    <>
      <PageHeader
        title="Agent controls"
        description="Merchant-set limits over a rolling window, category gates, and a kill switch that stops one agent immediately without affecting any other."
      />

      <Card
        title="Per-agent controls"
      >
        {agents.length === 0 ? (
          <Empty>No agent has presented a credential yet.</Empty>
        ) : (
          <Table head={["Agent", "Tier", "Window limits", "Category gates", "Kill switch"]}>
            {agents.map((agent) => (
              <Row key={agent.agent_id}>
                <Cell>
                  <div className="font-mono text-xs">{agent.agent_id}</div>
                  <div className="mt-1 font-mono text-xs text-muted">{agent.issuer_id}</div>
                </Cell>
                <Cell>
                  <Pill tone={agent.tier === "unverified" ? "escalate" : "allow"}>
                    {agent.tier}
                  </Pill>
                </Cell>
                <Cell>
                  <AgentLimits
                    agentId={agent.agent_id}
                    spendMinor={agent.max_spend_per_window.amount}
                    transactions={agent.max_transactions_per_window}
                    action={updateAgentLimits}
                  />
                </Cell>
                <Cell>
                  <CategoryGate
                    agentId={agent.agent_id}
                    categories={categories.categories}
                    blocked={agent.blocked_categories}
                    action={updateAgentLimits}
                  />
                </Cell>
                <Cell>
                  <KillSwitch
                    agentId={agent.agent_id}
                    enabled={agent.kill_switch}
                    action={setKillSwitch}
                  />
                </Cell>
              </Row>
            ))}
          </Table>
        )}
      </Card>

      <Card title="How these interact with the kernel">
        <div className="space-y-2 px-5 py-4 text-sm text-muted">
          <p>
            These limits are the merchant&apos;s, and they sit alongside the constraints the human
            set in the open mandate. The stricter of the two always wins: raising a limit here can
            never widen the authority a human granted.
          </p>
          <p>
            The window limits also feed structuring detection. Several transactions that each sit
            under the per-transaction cap but together breach it are refused as one attempt to
            evade the cap.
          </p>
        </div>
      </Card>
    </>
  );
}
