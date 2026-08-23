/** Shapes returned by the Dwarpal merchant API. */

export type Amount = {
  amount: number;
  currency: string;
  display: string;
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

export type AttackScorecard = {
  generated_at: string;
  merchant: string;
  adversarial: {
    total: number;
    blocked: number;
    passed: number;
    missed: number;
    block_rate: number;
  };
  benign: {
    total: number;
    allowed: number;
    escalated_to_human: number;
    false_positives: number;
    false_positive_rate: number;
  };
  families: string[];
  misses: Array<Record<string, unknown>>;
  false_positive_detail: Array<Record<string, unknown>>;
  results: Array<{
    id: string;
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
  disputes: Array<{
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
