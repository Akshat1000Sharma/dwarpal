import type { Metadata } from "next";
import Link from "next/link";

import { BrandHeading, CLAUDE_ORANGE, ClaudeMark, WHATSAPP_GREEN, WhatsAppMark } from "@/components/brand";
import { CopyBlock, CopyField } from "@/components/copy";
import { BackendDown, Card, Code, Note, PageHeader, Pill } from "@/components/ui";
import { backendReachable, backendRead } from "@/lib/backend";
import type { ConnectionsPayload, GatewayMode } from "@/lib/types";

export const dynamic = "force-dynamic";
export const metadata: Metadata = { title: "Configure your agent" };

export default async function SetupPage() {
  if (!(await backendReachable())) return <BackendDown />;

  const [gateway, connections] = await Promise.all([
    backendRead<GatewayMode | null>("/buyer/gateway", null),
    backendRead<ConnectionsPayload>("/merchant/connections", {
      connections: [],
      header: "X-Dwarpal-Connection",
      public_base_url: "http://localhost:8000",
    }),
  ]);

  const base = connections.public_base_url;
  const card = gateway?.test_card;

  return (
    <>
      <PageHeader
        title="Configure your AI agent"
        description="Everything an agent needs to transact here: where to look, what to present, and how the money moves. The console on the previous page is one client of this API; yours can be another."
      />

      <Note tone="brand">
        <span className="font-medium">Test money only.</span> Dwarpal refuses to start against a
        live Razorpay key, so nothing an agent does here can move real funds. The card below is
        Razorpay&apos;s published test card and is accepted by nothing else.
      </Note>

      <SectionRule
        eyebrow="Part one"
        title="The HTTP path"
        description="Six steps from arriving at the merchant to a settled payment. This is the surface every agent uses, whatever it is built on."
      />

      <Card
        title="1. Get a connection"
        description="A connection tells the merchant which agent is yours and where to send the WhatsApp receipt when it buys something."
      >
        <div className="space-y-4 px-4 py-5 sm:px-5">
          <p className="text-[13px] leading-relaxed text-body">
            Create one on the{" "}
            <Link href="/merchant/connections" className="text-brand hover:underline">
              connections page
            </Link>
            . You give it a label and a WhatsApp number in E.164 form; you get a token back exactly
            once. Send it on every request as <Code>{connections.header}</Code>.
          </p>
          <Note>
            The token identifies your agent and routes your notifications.{" "}
            <span className="font-medium">It grants no purchasing authority.</span> What an agent
            may buy comes from the mandates a human signed, and from nowhere else, so a leaked
            token cannot spend anything.
          </Note>
        </div>
      </Card>

      <Card
        title="2. Find the merchant"
        description="One well-known document tells an arriving agent everything it needs to negotiate: the protocol versions spoken, the credential types accepted, where to browse and quote, and which issuing authorities are trusted."
      >
        <div className="space-y-4 px-4 py-5 sm:px-5">
          <CopyField label="Discovery" value={`${base}/.well-known/ap2-merchant`} />
          <CopyBlock label="Try it" value={`curl -s ${base}/.well-known/ap2-merchant | jq`} />
          <p className="text-[12.5px] leading-relaxed text-muted">
            The <Code>audience</Code> in that document is the value your key-binding proof must
            carry. Do not guess it from the URL you dialled: the merchant may sit behind a proxy or
            a tunnel, and a proof addressed to the wrong audience is refused.
          </p>
        </div>
      </Card>

      <Card
        title="3. Browse, then quote"
        description="Browsing and quoting need no credentials at all. An agent assembles a cart first and identifies itself only when it wants to buy."
      >
        <div className="space-y-4 px-4 py-5 sm:px-5">
          <CopyBlock
            label="Browse the catalog"
            value={`curl -s "${base}/catalog/items?limit=10" \\
  -H "${connections.header}: dwc_your_token_here" | jq`}
          />
          <CopyBlock
            label="Ask for a price"
            value={`curl -s -X POST ${base}/checkout/quote \\
  -H "Content-Type: application/json" \\
  -H "${connections.header}: dwc_your_token_here" \\
  -d '{"items":[{"sku":"DWP-TEA-001","quantity":2}]}' | jq`}
          />
          <p className="text-[12.5px] leading-relaxed text-muted">
            The quote freezes the prices, holds the stock and returns a Checkout the merchant has
            signed. That signature is a commitment to fulfil at the stated SKU, price and shipping,
            and it is what every later step is compared against. It carries a{" "}
            <Code>checkout_jwt</Code>, a <Code>checkout_hash</Code> and the{" "}
            <Code>policy_hash</Code> your closed mandate has to acknowledge.
          </p>
        </div>
      </Card>

      <Card
        title="4. Present the four mandates"
        description="AP2 separates two questions and answers each at two moments. The open pair is the standing authority a human signed in advance; the closed pair is the agent's claim about this specific purchase."
      >
        <div className="space-y-4 px-4 py-5 sm:px-5">
          <div className="grid gap-3 sm:grid-cols-2">
            <Mandate
              title="Open Checkout Mandate"
              vct="mandate.checkout.open.1"
              signer="the human's trusted surface"
              carries="which merchants, which line items, the amount range, and the key it was issued to"
            />
            <Mandate
              title="Open Payment Mandate"
              vct="mandate.payment.open.1"
              signer="the human's trusted surface"
              carries="allowed payees, instruments, the budget and the execution window"
            />
            <Mandate
              title="Closed Checkout Mandate"
              vct="mandate.checkout.1"
              signer="the agent"
              carries="the checkout_jwt and checkout_hash from the quote, verbatim"
            />
            <Mandate
              title="Closed Payment Mandate"
              vct="mandate.payment.1"
              signer="the agent"
              carries="the transaction id, the payee, the exact amount and the instrument"
            />
          </div>

          <CopyBlock
            label="Complete the purchase"
            value={`curl -s -X POST ${base}/checkout/complete \\
  -H "Content-Type: application/json" \\
  -H "${connections.header}: dwc_your_token_here" \\
  -H "Idempotency-Key: your-own-unique-key" \\
  -d '{
    "open_checkout_mandate":  "<sd-jwt with key binding>",
    "closed_checkout_mandate":"<sd-jwt>",
    "open_payment_mandate":   "<sd-jwt with key binding>",
    "closed_payment_mandate": "<sd-jwt>",
    "nonce": "unique-per-attempt"
  }' | jq`}
          />

          <p className="text-[12.5px] leading-relaxed text-muted">
            The <Code>cnf.jwk</Code> claim in each open mandate names the key it was issued to, and
            you prove possession by signing a key-binding JWT with the matching private half. That
            is what defeats a stolen credential: presenting a genuine mandate issued to somebody
            else fails at <Code>CRED_SUBJECT_MISMATCH</Code>.
          </p>
          <Note>
            Do not hand-roll the mandates while you are getting started. The signing helper the
            tests, the corpus and the interop driver all share is{" "}
            <Code>backend/app/harness/factory.py</Code>. A complete worked client, about five
            hundred and fifty lines of it, is <Code>backend/interop/driver.py</Code>; run it with{" "}
            <Code>python interop/run_interop.py</Code>, which is only the entry point.
          </Note>
        </div>
      </Card>

      <Card
        title="5. Read the answer without parsing prose"
        description="Every response carries a reason code from a closed set and the action an agent should take next, so a refusal never needs a human to interpret it."
      >
        <div className="space-y-4 px-4 py-5 sm:px-5">
          <CopyBlock
            label="Enumerate every refusal you might see"
            value={`curl -s ${base}/merchant/reason-codes \\
  -H "${connections.header}: dwc_your_merchant_scoped_token" | jq`}
          />
          <p className="text-[12.5px] leading-relaxed text-muted">
            That endpoint is on the merchant control plane, so it needs a{" "}
            <Code>merchant</Code>-scoped connection token or the shared{" "}
            <Code>X-Merchant-Token</Code>. A buyer-scoped token is refused with a 401. It returns
            sixty-one codes, each mapped to exactly one of these six actions.
          </p>
          <div className="grid gap-2 sm:grid-cols-3">
            {[
              ["present_credentials", "your authority was not accepted"],
              ["reduce_cart", "the cart is outside what was allowed"],
              ["retry", "state moved under you; quote again"],
              ["wait", "a limit or a human will clear in time"],
              ["stop", "do not try this again"],
              ["proceed", "approved"],
            ].map(([action, meaning]) => (
              <div key={action} className="rounded-[9px] border border-line bg-sunken px-3 py-2.5">
                <div className="font-mono text-[11.5px] text-ink">{action}</div>
                <div className="mt-0.5 text-[11.5px] leading-snug text-muted">{meaning}</div>
              </div>
            ))}
          </div>
        </div>
      </Card>

      <Card
        title="6. Set up automated payments"
        description="The merchant creates the Razorpay order only after the kernel has approved. Nothing about paying can widen what was allowed."
      >
        <div className="space-y-4 px-4 py-5 sm:px-5">
          {card ? (
            <div className="grid gap-3 sm:grid-cols-3">
              <CopyField label="Card number" value={card.number} />
              <CopyField label="Expiry" value={card.expiry} />
              <CopyField label="CVV" value={card.cvv} />
            </div>
          ) : null}

          <ol className="space-y-2.5 text-[13px] leading-relaxed text-body">
            <li>
              <span className="font-medium text-ink">Approved.</span> A verdict is recorded and
              signed. No order exists before this point, and the payments service refuses to create
              one without an approving verdict id.
            </li>
            <li>
              <span className="font-medium text-ink">Order created.</span> The response carries{" "}
              <Code>detail.razorpay_order_id</Code> and status <Code>awaiting_payment</Code>.
            </li>
            <li>
              <span className="font-medium text-ink">Paid.</span> Open Razorpay Checkout on that
              order and enter the test card. The buyer console does exactly this.
            </li>
            <li>
              <span className="font-medium text-ink">Captured.</span> The merchant verifies the
              handler signature, an HMAC over <Code>order_id|payment_id</Code>, then captures and
              settles. A signed <Code>payment.captured</Code> webhook does the same job when a
              tunnel is running, and both arriving is harmless.
            </li>
            <li>
              <span className="font-medium text-ink">Told.</span> The principal gets a WhatsApp
              receipt naming what was bought, for how much, and by which agent.
            </li>
          </ol>

          <Note tone="escalate">
            The budget reservation is held, not committed, while an order is unpaid, and it expires
            on its own. An abandoned checkout does not consume the human&apos;s budget forever.
          </Note>
        </div>
      </Card>

      <SectionRule
        eyebrow="Part two"
        title="Connect agents with MCP"
        description="The merchant speaks the Model Context Protocol as well as HTTP, so an assistant can read the catalog and take a quote conversationally, without being taught the REST API first."
      />

      <Card>
        <div className="space-y-5 px-4 py-5 sm:px-5">
          <BrandHeading
            mark={<ClaudeMark colored className="h-6 w-6" title="Claude" />}
            tint={CLAUDE_ORANGE}
            eyebrow="Claude Desktop and Claude Code"
            title="Point Claude at the catalog"
          >
            This is the read-and-quote surface. Completing a purchase still goes through the HTTP
            endpoint above, where the full verification pipeline runs, so an assistant can shop but
            cannot settle on its own.
          </BrandHeading>

          <div>
            <div className="mb-2 text-[11px] font-semibold uppercase tracking-[0.07em] text-faint">
              Before you start
            </div>
            <ul className="space-y-1.5 text-[12.5px] leading-relaxed text-muted">
              <li>
                PostgreSQL must be running. The MCP server talks to the database directly, not
                through the HTTP backend, so <Code>docker compose up -d</Code> is enough; the
                uvicorn process does not have to be up.
              </li>
              <li>
                The dependencies live in the virtual environment the README tells you to create at{" "}
                <Code>backend/.venv</Code>. Point the config at that interpreter by absolute path.
                An MCP client spawns the command with your system PATH and never activates a
                virtual environment, so a bare <Code>python</Code> here fails with{" "}
                <Code>ModuleNotFoundError: No module named &apos;mcp&apos;</Code>.
              </li>
              <li>
                <Code>cwd</Code> must be the <Code>backend</Code> directory, or the{" "}
                <Code>app</Code> package cannot be imported.
              </li>
            </ul>
          </div>

          <CopyBlock
            label="Windows: claude_desktop_config.json or .mcp.json"
            value={`{
  "mcpServers": {
    "dwarpal": {
      "command": "C:/absolute/path/to/dwarpal/backend/.venv/Scripts/python.exe",
      "args": ["-m", "app.mcp.server"],
      "cwd": "C:/absolute/path/to/dwarpal/backend"
    }
  }
}`}
          />

          <CopyBlock
            label="macOS or Linux"
            value={`{
  "mcpServers": {
    "dwarpal": {
      "command": "/absolute/path/to/dwarpal/backend/.venv/bin/python",
      "args": ["-m", "app.mcp.server"],
      "cwd": "/absolute/path/to/dwarpal/backend"
    }
  }
}`}
          />

          <div>
            <div className="mb-2 text-[11px] font-semibold uppercase tracking-[0.07em] text-faint">
              Seven tools appear once it connects
            </div>
            <div className="flex flex-wrap gap-1.5">
              {[
                "merchant_profile",
                "browse_catalog",
                "search_catalog",
                "get_item",
                "list_categories",
                "get_policy_terms",
                "quote_cart",
              ].map((tool) => (
                <Pill key={tool}>{tool}</Pill>
              ))}
            </div>
            <p className="mt-3 text-[12.5px] leading-relaxed text-muted">
              Ask Claude to find something and quote it. <Code>quote_cart</Code> returns the same
              merchant-signed Checkout that step 3 returns, including the{" "}
              <Code>checkout_jwt</Code> and <Code>checkout_hash</Code>, so you can hand the quote
              straight to whatever holds your signing key.
            </p>
          </div>

          <div>
            <div className="mb-2 text-[11px] font-semibold uppercase tracking-[0.07em] text-faint">
              Or over streamable HTTP
            </div>
            <CopyBlock
              label="Serve it on a port instead of stdio"
              value={`cd backend
.venv/Scripts/python -m app.mcp.server --http --port 8765`}
            />
            <p className="mt-2.5 text-[12.5px] leading-relaxed text-muted">
              The endpoint is <Code>http://127.0.0.1:8765/mcp</Code>. Set{" "}
              <Code>MCP_PUBLIC_URL</Code> in <Code>backend/.env</Code> to the address you serve it
              on and the discovery document will advertise it under{" "}
              <Code>endpoints.mcp</Code>. Left unset, discovery omits it rather than publishing an
              address that answers 404.
            </p>
          </div>
        </div>
      </Card>

      <SectionRule
        eyebrow="Part three"
        title="Stay informed on WhatsApp"
        description="An agent that spends without telling anyone is the thing people are afraid of. Dwarpal reaches the human on the channel they already read."
      />

      <Card>
        <div className="space-y-5 px-4 py-5 sm:px-5">
          <BrandHeading
            mark={<WhatsAppMark colored className="h-6 w-6" title="WhatsApp" />}
            tint={WHATSAPP_GREEN}
            eyebrow="Meta WhatsApp Cloud API"
            title="Receipts and approvals"
          >
            The number you register on a connection is where that agent&apos;s messages go. Nothing
            is sent to a number that has not been registered.
          </BrandHeading>

          <div className="grid gap-3 sm:grid-cols-3">
            <Kind
              code="purchase_completed"
              when="An agent settled a payment"
              body="What was bought, for how much, and which agent did it."
            />
            <Kind
              code="purchase_refused"
              when="The kernel refused"
              body="The reason code, so the refusal is legible without opening the console."
            />
            <Kind
              code="purchase_compensated"
              when="Money moved, then had to be returned"
              body="A capture that could not be honoured and the refund that reversed it."
            />
          </div>

          <div>
            <div className="mb-2 text-[11px] font-semibold uppercase tracking-[0.07em] text-faint">
              Approving a purchase from your phone
            </div>
            <p className="text-[12.5px] leading-relaxed text-muted">
              When the kernel cannot decide alone it escalates, and the message carries Approve and
              Deny buttons. The reply comes back through the webhook and releases or refuses the
              checkout. An escalation that is never answered is a denial, not a pause: the timeout
              fails closed, so silence can never become a purchase.
            </p>
          </div>

          <div>
            <div className="mb-2 text-[11px] font-semibold uppercase tracking-[0.07em] text-faint">
              What you have to configure
            </div>
            <ul className="space-y-1.5 text-[12.5px] leading-relaxed text-muted">
              <li>
                <Code>META_ACCESS_TOKEN</Code> and <Code>META_PHONE_NUMBER_ID</Code> for the number
                that sends, and <Code>META_APP_SECRET</Code> so inbound webhook signatures can be
                checked.
              </li>
              <li>
                <Code>META_TEMPLATE_NAME</Code> for approvals and{" "}
                <Code>META_RECEIPT_TEMPLATE_NAME</Code> for receipts. The two names shipped in{" "}
                <Code>.env.example</Code> are the Utility templates this project had approved. Meta
                only allows free-form messages inside the twenty-four hour window after the person
                last replied; outside it, an approved template is the only thing that will
                deliver.
              </li>
              <li>
                <Code>NOTIFY_PURCHASE_RECEIPTS</Code> turns receipts off without touching the
                escalation path.
              </li>
            </ul>
            <Note tone="escalate">
              Delivery is logged either way. Every attempt writes a row with its route and status,
              and a failed send is recorded as failed rather than swallowed, so a receipt that
              never arrived is visible on the run page instead of being invisible.
            </Note>
          </div>
        </div>
      </Card>
    </>
  );
}

function SectionRule({
  eyebrow,
  title,
  description,
}: {
  eyebrow: string;
  title: string;
  description: string;
}) {
  return (
    <div className="mt-2 border-t border-line pt-7">
      <div className="text-[11px] font-semibold uppercase tracking-[0.08em] text-brand">
        {eyebrow}
      </div>
      <h2 className="mt-1.5 text-[22px] font-semibold tracking-[-0.02em] text-ink sm:text-[25px]">
        {title}
      </h2>
      <p className="mt-2 max-w-[68ch] text-[13.5px] leading-relaxed text-muted">{description}</p>
    </div>
  );
}

function Kind({ code, when, body }: { code: string; when: string; body: string }) {
  return (
    <div className="rounded-[10px] border border-line bg-sunken p-4">
      <div className="font-mono text-[11.5px] text-ink">{code}</div>
      <div className="mt-1.5 text-[12px] font-medium text-body">{when}</div>
      <p className="mt-1 text-[12px] leading-relaxed text-muted">{body}</p>
    </div>
  );
}

function Mandate({
  title,
  vct,
  signer,
  carries,
}: {
  title: string;
  vct: string;
  signer: string;
  carries: string;
}) {
  return (
    <div className="rounded-[10px] border border-line bg-sunken p-4">
      <h3 className="text-[13px] font-semibold text-ink">{title}</h3>
      <p className="mt-1 font-mono text-[11px] text-brand">{vct}</p>
      <p className="mt-2 text-[12px] leading-relaxed text-muted">
        <span className="text-body">Signed by</span> {signer}
      </p>
      <p className="mt-1 text-[12px] leading-relaxed text-muted">
        <span className="text-body">Carries</span> {carries}
      </p>
    </div>
  );
}
