import type { Metadata } from "next";

import { ProductImage } from "@/components/product-image";
import { BackendDown, Card, Empty, PageHeader, Pill } from "@/components/ui";
import { backendReachable, backendRead } from "@/lib/backend";
import { money } from "@/lib/format";
import type { CatalogItem } from "@/lib/types";

export const dynamic = "force-dynamic";
export const metadata: Metadata = { title: "Catalog" };

export default async function CatalogPage() {
  if (!(await backendReachable())) return <BackendDown />;

  const { items } = await backendRead<{ items: CatalogItem[] }>("/merchant/catalog", { items: [] });
  const categories = [...new Set(items.map((item) => item.category))].sort();

  return (
    <>
      <PageHeader
        title="Catalog"
        description="What the merchant sells, rendered the way an agent reads it. Every item carries machine-readable purchase constraints, and those constraints are what the kernel gates on rather than anything in the description."
      />

      {items.length === 0 ? (
        <Card>
          <Empty>The catalog is empty. Seed it with the backend running.</Empty>
        </Card>
      ) : (
        categories.map((category, categoryIndex) => (
          <Card key={category} title={category}>
            <div className="grid gap-px bg-[color:var(--line)] sm:grid-cols-2 xl:grid-cols-3">
              {items
                .filter((item) => item.category === category)
                .map((item, index) => (
                  <Item
                    key={item.sku}
                    item={item}
                    priority={categoryIndex === 0 && index < 3}
                  />
                ))}
            </div>
          </Card>
        ))
      )}
    </>
  );
}

function Item({ item, priority }: { item: CatalogItem; priority: boolean }) {
  const constraints = item.purchase_constraints;
  const available = item.availability.available_quantity;

  return (
    <article className="flex flex-col bg-surface">
      <ProductImage
        src={item.image?.url}
        alt={item.image?.alt}
        title={item.title}
        category={item.category}
        priority={priority}
        className="aspect-[4/3] w-full"
      />

      <div className="flex flex-1 flex-col p-4 sm:p-5">
        <div className="flex items-start justify-between gap-3">
          <h3 className="text-[14px] font-medium leading-snug text-ink">{item.title}</h3>
          <span className="shrink-0 text-[14px] font-semibold tabular-nums text-ink">
            {money(item.price.amount, item.price.currency)}
          </span>
        </div>
        <p className="mt-1 font-mono text-[11px] text-faint">{item.sku}</p>
        <p className="mt-2.5 flex-1 text-[12.5px] leading-relaxed text-muted">
          {item.description}
        </p>

        <div className="mt-3.5 flex flex-wrap gap-1.5">
          <Pill tone={available > 0 ? "allow" : "deny"}>
            {available > 0 ? `${available} available` : "sold out"}
          </Pill>
          <Pill>
            {constraints.min_order_quantity} to {constraints.max_order_quantity} per order
          </Pill>
          {constraints.returnable ? (
            <Pill>returnable, {constraints.return_window_days}d</Pill>
          ) : (
            <Pill tone="escalate">not returnable</Pill>
          )}
          {constraints.age_restricted && <Pill tone="deny">age restricted</Pill>}
          {constraints.restricted_category && <Pill tone="deny">restricted category</Pill>}
          {constraints.perishable && <Pill tone="escalate">perishable</Pill>}
          {constraints.region_locked.length > 0 && (
            <Pill tone="escalate">blocked in {constraints.region_locked.join(", ")}</Pill>
          )}
        </div>
      </div>
    </article>
  );
}
