/** Shapes returned by the Dwarpal merchant API. */

export type Amount = {
  amount: number;
  currency: string;
  display: string;
};

/**
 * What the catalog document carries. CatalogEntry.as_document() emits minor units and a currency
 * and no rendered string, because it is also the agent-facing and MCP document, where a formatted
 * price would be a second source of truth. The console formats it with money().
 */
export type MinorAmount = {
  amount: number;
  currency: string;
};

export type Overview = {
  window_hours: number;
  verdicts: { allow: number; deny: number; escalate: number; challenge: number; total: number };
  captured: Amount;
  refunded: Amount;
  active_agents: number;
  open_mandates: number;
  pending_escalations: number;
  open_exceptions: number;
  evidence_packets: number;
  merchant: { id: string; name: string };
};

export type TrafficRow = {
  agent_id: string;
  display_name: string;
  issuer_id: string;
  tier: string;
  kill_switch: boolean;
  window_seconds: number;
  window_spend: Amount;
  window_transactions: number;
  budget_total: Amount;
  budget_used: Amount;
  budget_remaining: Amount;
  open_mandates: number;
  last_verdict: { decision: string; reason_code: string; at: string } | null;
};

export type Verdict = {
  id: string;
  correlation_id: string;
  checkout_id: string | null;
  agent_id: string;
  action: string;
  decision: string;
  reason_code: string;
  agent_action: string;
  amount: Amount;
  evidence: Record<string, unknown>;
  created_at: string;
};

export type Mandate = {
  id: string;
  kind: string;
  digest: string;
  agent_id: string;
  issuer_id: string;
  tier: string;
  constraints: Array<Record<string, unknown>>;
  extension_constraints: Array<Record<string, unknown>>;
  cap: Amount;
  committed: Amount;
  reserved: Amount;
  remaining: Amount;
  use_count: number;
  expires_at: string | null;
  revoked_at: string | null;
  revoked_reason: string | null;
  created_at: string;
};

export type Agent = {
  agent_id: string;
  display_name: string;
  issuer_id: string;
  tier: string;
  kill_switch: boolean;
  max_spend_per_window: Amount;
  max_transactions_per_window: number;
  allowed_categories: string[];
  blocked_categories: string[];
  created_at: string;
};

export type EscalationResponseRow = {
  answer: string;
  accepted: boolean;
  ignored_reason: string | null;
  received_at: string;
};

export type Escalation = {
  escalation_id: string;
  raised_reason: string;
  constraint: string;
  amount: { amount: number; currency: string };
  cart_fingerprint: string;
  status: string;
  created_at: string;
  deadline_at: string;
  answered_at: string | null;
  channel_message_id: string | null;
  delivery_error: string | null;
  responses: EscalationResponseRow[];
};

export type ChainReport = {
  packets: number;
  valid: boolean;
  problems: Array<{ seq: number; problem: string }>;
};

export type EvidenceSummary = {
  seq: number;
  packet_id: string;
  correlation_id: string;
  outcome: string | null;
  agent_id: string | null;
  created_at: string;
};

export type EvidencePacket = {
  seq: number;
  packet_id: string;
  prev_hash: string;
  entry_hash: string;
  signature: string;
  created_at: string;
  body: EvidenceBody;
};

export type EvidenceBody = {
  schema: string;
  correlation_id: string;
  outcome: string;
  agent_id: string;
  recorded_at: string;
  merchant: { id: string; name: string; jwks: unknown };
  protocol: { ap2_version: string; schema_revision: string };
  credential_chain: Record<string, string | null>;
  verification: Record<string, unknown>;
  checkout: Record<string, unknown>;
  verdicts: Array<Record<string, unknown>>;
  semantic_checks: Array<Record<string, unknown>>;
  escalations: Escalation[];
  payments: Array<Record<string, unknown>>;
  refunds: Array<Record<string, unknown>>;
  timings: Array<{ step: string; started_at: string; finished_at: string | null; duration_ms: number | null }>;
  extra: Record<string, unknown>;
};

export type DisputeSummary = {
  id: string;
  correlation_id: string;
  claim: string;
  recommendation: string | null;
  strength_score: number | null;
  outcome: string;
  claimed_at: string;
  decided_at: string | null;
};

export type EvidenceFactor = {
  key: string;
  description: string;
  weight: number;
  present: boolean;
  awarded: number;
  detail: string;
};

export type Representment = {
  correlation_id: string;
  recommendation: string;
  strength_score: number;
  contest_threshold: number;
  factors: EvidenceFactor[];
  narrative: string[];
  weaknesses: string[];
  timeline: Array<{ at: string; event: string }>;
  evidence_packets: string[];
};

export type DisputeDetail = DisputeSummary & { representment: Representment };

export type PaymentExceptionRow = {
  id: string;
  correlation_id: string;
  payment_id: string | null;
  kind: string;
  local_state: Record<string, unknown>;
  gateway_state: Record<string, unknown>;
  resolved: boolean;
  created_at: string;
};

export type CheckoutRow = {
  id: string;
  correlation_id: string;
  agent_id: string;
  state: string;
  total: Amount;
  verified: boolean;
  created_at: string;
  expires_at: string;
};

/**
 * A technique is one attack idea. A case is that idea executed against one item, issuing tier and
 * amount. Both are reported, and neither stands in for the other.
 *
 * The per-case `results` list is only sent when the endpoint is asked for it with `full=true`,
 * because it is most of the artifact and only the scorecards page renders it.
 */
export type AttackScorecard = {
  generated_at: string;
  merchant: string;
  adversarial: {
    total: number;
    techniques: number;
    blocked: number;
    passed: number;
    missed: number;
    block_rate: number;
  };
  benign: {
    total: number;
    techniques: number;
    allowed: number;
    escalated_to_human: number;
    false_positives: number;
    false_positive_rate: number;
    settled_without_asking: number;
  };
  families: string[];
  techniques: string[];
  by_technique: Array<{
    technique: string;
    family: string;
    cases: number;
    blocked: number;
    passed: number;
    missed: number;
  }>;
  misses: Array<Record<string, unknown>>;
  false_positive_detail: Array<Record<string, unknown>>;
  settled_without_asking_detail: Array<Record<string, unknown>>;
  results_count: number;
  results?: Array<{
    id: string;
    technique: string;
    family: string;
    kind: string;
    description: string;
    observed_blocked: boolean;
    observed_reason_code: string;
    passed: boolean;
  }>;
};

export type DisputeReport = {
  generated_at: string;
  total: number;
  with_evidence: { defensible: number; defence_rate: number; mean_strength: number };
  baseline: { defensible: number; defence_rate: number; mean_strength: number };
  improvement: number;
  refund_recommended: Array<{ case_id: string; strength_score: number; weaknesses: string[] }>;
  disputes_count: number;
  disputes?: Array<{
    case_id: string;
    correlation_id: string;
    transaction_outcome: string;
    strength_score: number;
    recommendation: string;
    baseline_score: number;
  }>;
};

export type Reports = {
  generated: boolean;
  attack_scorecard: AttackScorecard | null;
  dispute_defence: DisputeReport | null;
};

export type ReasonCodeRow = { code: string; agent_action: string };

export type ConnectionScope = "buyer" | "merchant";

export type Connection = {
  id: string;
  label: string;
  scope: ConnectionScope;
  agent_id: string;
  whatsapp: string | null;
  token_prefix: string;
  notify_completed: boolean;
  notify_refused: boolean;
  revoked: boolean;
  created_at: string;
  last_used_at: string | null;
  endpoints: Record<string, string>;
  header: string;
  /** Present exactly once, in the response that minted the connection. */
  token?: string;
  token_shown_once?: boolean;
};

export type ConnectionsPayload = {
  connections: Connection[];
  header: string;
  public_base_url: string;
};

export type NotificationRow = {
  id: string;
  correlation_id: string;
  kind: string;
  route: string;
  to: string | null;
  status: string;
  provider_message_id: string | null;
  error: string | null;
  summary: string;
  created_at: string;
};

export type BuyerPlanLine = { sku: string; title: string; quantity: number };

export type StockedCatalogItem = CatalogItem & { stock_total: number };

export type BuyerPlan = {
  planner: string;
  lines: BuyerPlanLine[];
  budget_cap_minor: number;
  natural_language: string[];
  rationale: string;
  dropped: Array<{ sku: string; why: string }>;
  estimated_total_minor: number;
};

export type BuyerRunEvent = {
  seq: number;
  level: string;
  step: string;
  message: string;
  data: Record<string, unknown>;
  duration_ms: number | null;
  at: string;
};

export type BuyerRunSummary = {
  id: string;
  prompt: string;
  planner: string;
  agent_id: string;
  status: string;
  correlation_id: string;
  checkout_id: string | null;
  razorpay_order_id: string | null;
  reason_code: string | null;
  evidence_packet_id: string | null;
  amount: Amount;
  plan: BuyerPlan | Record<string, never>;
  created_at: string;
  finished_at: string | null;
};

export type BuyerRunDetail = BuyerRunSummary & {
  events: BuyerRunEvent[];
  receipts: Array<{ kind: string; status: string; route: string; error: string | null; at: string }>;
};

export type GatewayMode = {
  mode: "razorpay" | "stub";
  key_id: string | null;
  test_card: { number: string; expiry: string; cvv: string; name: string; note: string };
  merchant: { id: string; name: string };
  explanation: string;
};

export type BuyerDefaults = {
  budget_cap_minor: number;
  suggested_prompts: string[];
  constraints: string[];
};

export type CatalogItem = {
  sku: string;
  title: string;
  description: string;
  /** Null when the seed has no picture for this item; the UI draws a placeholder instead. */
  image: { url: string; alt: string } | null;
  category: string;
  price: MinorAmount;
  availability: { in_stock: boolean; available_quantity: number; stock_total?: number };
  purchase_constraints: {
    min_order_quantity: number;
    max_order_quantity: number;
    returnable: boolean;
    return_window_days: number;
    age_restricted: boolean;
    region_locked: string[];
    restricted_category: boolean;
    perishable: boolean;
  };
  attributes: Record<string, unknown>;
};
