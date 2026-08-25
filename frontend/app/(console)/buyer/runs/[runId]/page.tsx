import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";

import { RunStatus } from "@/app/(console)/buyer/page";
import { ProductImage } from "@/components/product-image";
import { RunLog } from "@/components/run-log";
import { BackendDown, Card, Cell, PageHeader, Row, Table } from "@/components/ui";
import { backendFetch, backendReachable, backendRead } from "@/lib/backend";
import { relative, timestamp } from "@/lib/format";
import type { BuyerRunDetail, CatalogItem, GatewayMode } from "@/lib/types";

export const dynamic = "force-dynamic";
export const metadata: Metadata = { title: "Agent run" };

/**
 * params is a promise in Next 16. The shape is written out rather than using the generated
 * PageProps helper, because that helper only exists once a build has emitted .next/types and the
 * type check runs before the build.
 */
type Props = { params: Promise<{ runId: string }> };

export default async function RunPage({ params }: Props) {
  const { runId } = await params;
  if (!(await backendReachable())) return <BackendDown />;

  let run: BuyerRunDetail;
  try {
    run = await backendFetch<BuyerRunDetail>(`/buyer/runs/${runId}`);
  } catch {
    notFound();
  }

  const [gateway, catalog] = await Promise.all([
    backendRead<GatewayMode | null>("/buyer/gateway", null),
    backendRead<{ items: CatalogItem[] }>("/merchant/catalog", { items: [] }),
  ]);
  const plan = "lines" in run.plan ? run.plan : null;
  // The plan records what was bought, not what it looked like. Joining on the SKU keeps the cart
  // looking like the catalog it came from rather than a bare list of identifiers.
  const bySku = new Map(catalog.items.map((item) => [item.sku, item]));

  return (
    <>
      <PageHeader
        title="Agent run"
        description={run.prompt}
        actions={
          <Link href="/buyer" className="text-[13px] text-brand hover:underline">
            Send another
          </Link>
        }
      />

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Fact label="Status" value={<RunStatus status={run.status} />} />
        <Fact
          label="Reason code"
          value={
            run.reason_code ? (
              <span className="font-mono text-[12px] text-ink">{run.reason_code}</span>
            ) : (
              <span className="text-muted">-</span>
            )
          }
        />
        <Fact label="Amount" value={<span className="tabular-nums">{run.amount.display}</span>} />
        <Fact
          label="Planner"
          value={<span className="font-mono text-[12px]">{run.planner}</span>}
        />
      </div>

      <RunLog initial={run} gateway={gateway} />

      {plan && plan.lines.length > 0 && (
        <Card
          title="What the agent chose"
          description={plan.rationale || undefined}
        >
          <Table head={["Item", "SKU", "Quantity"]}>
            {plan.lines.map((line) => {
              const item = bySku.get(line.sku);
              return (
                <Row key={line.sku}>
                  <Cell label="Item">
                    <span className="flex items-center gap-3">
                      <ProductImage
                        src={item?.image?.url}
                        alt={item?.image?.alt}
                        title={line.title}
                        category={item?.category ?? ""}
                        sizes="48px"
                        className="h-12 w-12 shrink-0 rounded-[8px] border border-line"
                      />
                      <span className="min-w-0 text-left">{line.title}</span>
                    </span>
                  </Cell>
                  <Cell label="SKU" mono>
                    {line.sku}
                  </Cell>
                  <Cell label="Quantity">
                    <span className="tabular-nums">{line.quantity}</span>
                  </Cell>
                </Row>
              );
            })}
          </Table>
          {plan.dropped.length > 0 && (
            <div className="border-t border-line px-4 py-4 sm:px-5">
              <div className="text-[11px] font-semibold uppercase tracking-[0.07em] text-faint">
                Adjusted before quoting
              </div>
              <ul className="mt-2 space-y-1.5 text-[12.5px] leading-relaxed text-muted">
                {plan.dropped.map((item, index) => (
                  <li key={`${item.sku}-${index}`}>
                    <span className="font-mono text-[12px] text-ink">{item.sku}</span> - {item.why}
                  </li>
                ))}
              </ul>
              <p className="mt-3 text-[11.5px] leading-relaxed text-faint">
                Whatever the planner proposes is re-read from the catalog before anything is
                quoted. A model that invented a SKU or a quantity cannot put either on the wire.
              </p>
            </div>
          )}
        </Card>
      )}

      {run.receipts.length > 0 && (
        <Card
          title="WhatsApp receipts"
          description="What the principal was told about this purchase, whether or not it was delivered."
        >
          <Table head={["Kind", "Route", "Status", "When"]}>
            {run.receipts.map((receipt, index) => (
              <Row key={`${receipt.kind}-${index}`}>
                <Cell label="Kind">{receipt.kind.replace(/_/g, " ")}</Cell>
                <Cell label="Route" mono>
                  {receipt.route}
                </Cell>
                <Cell label="Status">
                  <span className={receipt.status === "sent" ? "text-allow" : "text-escalate"}>
                    {receipt.status}
                  </span>
                  {receipt.error && (
                    <span className="mt-1 block text-[11.5px] text-muted">{receipt.error}</span>
                  )}
                </Cell>
                <Cell label="When">{relative(receipt.at)}</Cell>
              </Row>
            ))}
          </Table>
        </Card>
      )}

      <Card title="Where to find this transaction elsewhere">
        <dl className="grid gap-4 px-4 py-5 text-[13px] sm:grid-cols-2 sm:px-5">
          <Trace label="Correlation id" value={run.correlation_id} />
          <Trace label="Checkout id" value={run.checkout_id ?? "-"} />
          <Trace label="Razorpay order" value={run.razorpay_order_id ?? "-"} />
          <Trace label="Evidence packet" value={run.evidence_packet_id ?? "-"} />
          <Trace label="Started" value={timestamp(run.created_at)} />
          <Trace label="Finished" value={run.finished_at ? timestamp(run.finished_at) : "-"} />
        </dl>
        <div className="border-t border-line px-4 py-4 text-[12.5px] text-muted sm:px-5">
          Every log line, verdict, payment record and evidence packet for this purchase carries
          that correlation id.{" "}
          <Link
            href={`/merchant/verdicts?agent_id=${encodeURIComponent(run.agent_id)}`}
            className="text-brand hover:underline"
          >
            See it in the merchant verdict log
          </Link>
          .
        </div>
      </Card>
    </>
  );
}

function Fact({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="rounded-[14px] border border-line bg-surface px-4 py-4 shadow-e1">
      <div className="text-[11px] font-semibold uppercase tracking-[0.06em] text-faint">
        {label}
      </div>
      <div className="mt-2 text-[15px] text-ink">{value}</div>
    </div>
  );
}

function Trace({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-[11px] font-semibold uppercase tracking-[0.06em] text-faint">{label}</dt>
      <dd className="mt-1 truncate font-mono text-[12px] text-ink" title={value}>
        {value}
      </dd>
    </div>
  );
}
