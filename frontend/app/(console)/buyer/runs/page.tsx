import type { Metadata } from "next";
import Link from "next/link";

import { RunStatus } from "@/app/(console)/buyer/page";
import { BackendDown, Card, Cell, Empty, PageHeader, Row, Table } from "@/components/ui";
import { backendReachable, backendRead } from "@/lib/backend";
import { relative } from "@/lib/format";
import type { BuyerRunSummary } from "@/lib/types";

export const dynamic = "force-dynamic";
export const metadata: Metadata = { title: "Agent runs" };

export default async function RunsPage() {
  if (!(await backendReachable())) return <BackendDown />;

  const { runs } = await backendRead<{ runs: BuyerRunSummary[] }>("/buyer/runs?limit=50", {
    runs: [],
  });

  return (
    <>
      <PageHeader
        title="Agent runs"
        description="Every purchase attempt this console has driven, and what the merchant decided about it."
        actions={
          <Link
            href="/buyer"
            className="rounded-[8px] bg-brand px-3.5 py-2 text-[13px] font-medium text-white transition-colors hover:bg-brand-strong"
          >
            Send an agent
          </Link>
        }
      />

      <Card>
        {runs.length === 0 ? (
          <Empty>
            No agent has been sent yet. Start one from{" "}
            <Link href="/buyer" className="text-brand hover:underline">
              the buyer console
            </Link>
            .
          </Empty>
        ) : (
          <Table head={["Instruction", "Agent", "Status", "Reason", "Amount", "Started"]}>
            {runs.map((run) => (
              <Row key={run.id}>
                <Cell label="Instruction">
                  <Link href={`/buyer/runs/${run.id}`} className="text-brand hover:underline">
                    {run.prompt}
                  </Link>
                </Cell>
                <Cell label="Agent" mono>
                  {run.agent_id}
                </Cell>
                <Cell label="Status">
                  <RunStatus status={run.status} />
                </Cell>
                <Cell label="Reason" mono>
                  {run.reason_code ?? "-"}
                </Cell>
                <Cell label="Amount">
                  <span className="tabular-nums">{run.amount.display}</span>
                </Cell>
                <Cell label="Started">{relative(run.created_at)}</Cell>
              </Row>
            ))}
          </Table>
        )}
      </Card>
    </>
  );
}
