import type { Metadata } from "next";
import Link from "next/link";

import {
  BrandChip,
  CLAUDE_ORANGE,
  ClaudeMark,
  WHATSAPP_GREEN,
  WhatsAppMark,
} from "@/components/brand";
import { ConnectionForm, RevokeConnection } from "@/components/connection-form";
import {
  BackendDown,
  Card,
  Cell,
  Empty,
  Note,
  PageHeader,
  Pill,
  Row,
  Table,
} from "@/components/ui";
import { backendReachable, backendRead } from "@/lib/backend";
import { relative } from "@/lib/format";
import type { ConnectionsPayload, NotificationRow } from "@/lib/types";

export const dynamic = "force-dynamic";
export const metadata: Metadata = { title: "Connect an agent" };

export default async function ConnectionsPage() {
  if (!(await backendReachable())) return <BackendDown />;

  const [connections, notifications] = await Promise.all([
    backendRead<ConnectionsPayload>("/merchant/connections", {
      connections: [],
      header: "X-Dwarpal-Connection",
      public_base_url: "http://localhost:8000",
    }),
    backendRead<{ notifications: NotificationRow[] }>("/merchant/notifications", {
      notifications: [],
    }),
  ]);

  const live = connections.connections.filter((c) => !c.revoked);

  return (
    <>
      <PageHeader
        title="Connect an agent"
        description="Bring your own agent, for buying or for selling. A connection gives it an identity here and gives you a number to be told on. It never gives it authority to spend."
      />

      <Note>
        <span className="font-medium">What a connection is, and is not.</span> It answers two
        questions: whose agent is this, and where do we tell them what it did. Purchasing authority
        comes from the credential chain a human signed, so a stolen connection token buys nothing.
        A merchant-scoped token reaches the control plane and can revoke a mandate or stop an
        agent, so treat that one the way you would treat the shared merchant secret.
      </Note>

      <div className="grid gap-3 sm:grid-cols-2">
        <BrandChip
          mark={<ClaudeMark colored className="h-6 w-6" title="Claude" />}
          tint={CLAUDE_ORANGE}
          label="Works with Claude over MCP"
          detail="Browse, search and quote as tools. Settling still runs the full pipeline."
        />
        <BrandChip
          mark={<WhatsAppMark colored className="h-6 w-6" title="WhatsApp" />}
          tint={WHATSAPP_GREEN}
          label="Receipts over WhatsApp"
          detail="The number on a connection is the only place its messages go."
        />
      </div>

      <Card
        title="Create a connection"
        description="Two kinds. A buyer connection lets an agent shop here; a merchant connection lets one run the shop."
      >
        <ConnectionForm header={connections.header} />
      </Card>

      <Card
        title="Your connections"
        description="Numbers are masked. Revoking takes effect on the very next request, with no cache in the way."
        actions={
          <Link href="/buyer/setup" className="text-[12px] text-brand hover:underline">
            How to wire one up
          </Link>
        }
      >
        {connections.connections.length === 0 ? (
          <Empty>No connection has been created yet.</Empty>
        ) : (
          <Table head={["Label", "Scope", "Agent", "WhatsApp", "Last used", ""]}>
            {connections.connections.map((connection) => (
              <Row key={connection.id}>
                <Cell label="Label">
                  <span className={connection.revoked ? "text-muted line-through" : "text-ink"}>
                    {connection.label}
                  </span>
                  <span className="mt-1 block font-mono text-[11px] text-faint">
                    {connection.token_prefix}...
                  </span>
                </Cell>
                <Cell label="Scope">
                  <Pill tone={connection.scope === "merchant" ? "challenge" : "brand"}>
                    {connection.scope}
                  </Pill>
                </Cell>
                <Cell label="Agent" mono>
                  {connection.agent_id}
                </Cell>
                <Cell label="WhatsApp" mono>
                  {connection.whatsapp ?? "-"}
                </Cell>
                <Cell label="Last used">
                  {connection.last_used_at ? relative(connection.last_used_at) : "never"}
                </Cell>
                <Cell label="">
                  {connection.revoked ? (
                    <Pill tone="deny">revoked</Pill>
                  ) : (
                    <RevokeConnection connectionId={connection.id} />
                  )}
                </Cell>
              </Row>
            ))}
          </Table>
        )}
      </Card>

      <Card
        title="Purchase receipts"
        actions={<WhatsAppMark colored className="h-[18px] w-[18px]" title="WhatsApp" />}
        description="Every attempt to tell somebody what an agent did with their authority, delivered or not. A receipt nobody got is a fact the merchant should be able to show."
      >
        {notifications.notifications.length === 0 ? (
          <Empty>
            Nothing has been sent yet. Receipts are raised when a purchase completes, is refused,
            or is reversed.
          </Empty>
        ) : (
          <Table head={["Outcome", "To", "Route", "Status", "When"]}>
            {notifications.notifications.slice(0, 25).map((row) => (
              <Row key={row.id}>
                <Cell label="Outcome">{row.kind.replace(/_/g, " ")}</Cell>
                <Cell label="To" mono>
                  {row.to ?? "-"}
                </Cell>
                <Cell label="Route" mono>
                  {row.route}
                </Cell>
                <Cell label="Status">
                  <Pill
                    tone={
                      row.status === "sent"
                        ? "allow"
                        : row.status === "failed"
                          ? "deny"
                          : "escalate"
                    }
                  >
                    {row.status}
                  </Pill>
                  {row.error && (
                    <span className="mt-1 block text-[11.5px] leading-snug text-muted">
                      {row.error}
                    </span>
                  )}
                </Cell>
                <Cell label="When">{relative(row.created_at)}</Cell>
              </Row>
            ))}
          </Table>
        )}
        <div className="border-t border-line px-4 py-4 text-[12px] leading-relaxed text-muted sm:px-5">
          A refusal is only sent to somebody who registered the agent. Falling back to the
          configured principal on every refusal would mean a hostile scan sends hundreds of
          messages nobody can act on, which is how a useful notification becomes an alarm that gets
          muted. Money that actually moved is always reported.
        </div>
      </Card>

      {live.length > 0 && (
        <Card title="What a connected agent can reach">
          <div className="grid gap-px bg-[color:var(--line)] sm:grid-cols-2">
            {Object.entries(live[0].endpoints).map(([name, url]) => (
              <div key={name} className="bg-surface px-4 py-3 sm:px-5">
                <div className="text-[11px] font-semibold uppercase tracking-[0.06em] text-faint">
                  {name.replace(/_/g, " ")}
                </div>
                <div className="mt-1 truncate font-mono text-[11.5px] text-ink" title={url}>
                  {url}
                </div>
              </div>
            ))}
          </div>
        </Card>
      )}
    </>
  );
}
