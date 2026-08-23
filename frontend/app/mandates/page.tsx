import { revokeMandate } from "@/app/actions";
import { RevokeMandate } from "@/components/controls";
import { BackendDown, Card, Cell, Empty, Json, Meter, Pill, Row, Table } from "@/components/ui";
import { backendReachable, backendRead } from "@/lib/backend";
import { relative, shorten, timestamp } from "@/lib/format";
import type { Mandate } from "@/lib/types";

export const dynamic = "force-dynamic";

function constraintSummary(mandate: Mandate): string[] {
  const lines: string[] = [];
  for (const constraint of mandate.constraints) {
    const type = String(constraint.type ?? "unknown");
    if (type === "checkout.allowed_merchants") {
      const allowed = (constraint.allowed as Array<{ id?: string }> | undefined) ?? [];
      lines.push(`merchants: ${allowed.map((m) => m.id).join(", ") || "not disclosed"}`);
    } else if (type === "checkout.line_items") {
      const items = (constraint.items as Array<{ quantity?: number }> | undefined) ?? [];
      lines.push(`${items.length} line-item requirement(s)`);
    } else {
      lines.push(type);
    }
  }
  for (const extension of mandate.extension_constraints) {
    lines.push(`natural language: ${String(extension.text ?? "")}`);
  }
  return lines;
}

export default async function MandatesPage() {
  if (!(await backendReachable())) return <BackendDown />;

  const { mandates } = await backendRead<{ mandates: Mandate[] }>("/merchant/mandates", {
    mandates: [],
  });

  return (
    <Card
      title="Open mandates in force"
      description="The authority each human granted, what it constrains, how much of it has been consumed, and when it expires. Revoking one stops it at its next use; a revocation that lands after capture is compensated automatically."
    >
      {mandates.length === 0 ? (
        <Empty>No open mandate has been presented yet.</Empty>
      ) : (
        <Table
          head={["Agent", "Issuer", "Constraints", "Consumption", "State", "Control"]}
        >
          {mandates.map((mandate) => (
            <Row key={mandate.id}>
              <Cell>
                <div className="font-mono text-xs">{mandate.agent_id}</div>
                <div className="mt-1 text-xs text-muted">
                  digest {shorten(mandate.digest, 8, 6)}
                </div>
              </Cell>
              <Cell>
                <div className="font-mono text-xs">{mandate.issuer_id}</div>
                <div className="mt-1">
                  <Pill tone={mandate.tier === "unverified" ? "escalate" : "allow"}>
                    {mandate.tier}
                  </Pill>
                </div>
              </Cell>
              <Cell>
                <ul className="space-y-1 text-xs text-muted">
                  {constraintSummary(mandate).map((line) => (
                    <li key={line}>{line}</li>
                  ))}
                </ul>
                <details className="mt-2">
                  <summary className="cursor-pointer text-xs text-accent">raw</summary>
                  <div className="mt-2 max-w-lg">
                    <Json
                      value={{
                        constraints: mandate.constraints,
                        dwarpal_constraints: mandate.extension_constraints,
                      }}
                    />
                  </div>
                </details>
              </Cell>
              <Cell>
                <Meter
                  used={mandate.committed.amount + mandate.reserved.amount}
                  total={mandate.cap.amount}
                  label={`${mandate.committed.display} committed, ${mandate.reserved.display} held, ${mandate.remaining.display} left`}
                />
                <div className="mt-1 text-xs text-muted">used {mandate.use_count} time(s)</div>
              </Cell>
              <Cell>
                {mandate.revoked_at ? (
                  <div>
                    <Pill tone="deny">revoked</Pill>
                    <div className="mt-1 text-xs text-muted">
                      {relative(mandate.revoked_at)}
                    </div>
                    {mandate.revoked_reason && (
                      <div className="mt-1 text-xs text-muted">{mandate.revoked_reason}</div>
                    )}
                  </div>
                ) : (
                  <div>
                    <Pill tone="allow">in force</Pill>
                    <div className="mt-1 text-xs text-muted">
                      {mandate.expires_at
                        ? `expires ${timestamp(mandate.expires_at)}`
                        : "no expiry set"}
                    </div>
                  </div>
                )}
              </Cell>
              <Cell>
                <RevokeMandate
                  mandateId={mandate.id}
                  revoked={Boolean(mandate.revoked_at)}
                  action={revokeMandate}
                />
              </Cell>
            </Row>
          ))}
        </Table>
      )}
    </Card>
  );
}
