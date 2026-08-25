"use server";

import { revalidatePath } from "next/cache";

import { backendFetch } from "@/lib/backend";

/**
 * The dashboard's only write actions: the merchant controls.
 *
 * These run on the server and go through the same server-only module every read uses, so the
 * backend URL still exists in exactly one place.
 */

export type ActionResult = { ok: boolean; message: string };

function failure(error: unknown): ActionResult {
  return { ok: false, message: String(error instanceof Error ? error.message : error).slice(0, 300) };
}

export async function revokeMandate(mandateId: string, reason: string): Promise<ActionResult> {
  try {
    await backendFetch(`/merchant/mandates/${mandateId}/revoke`, {
      method: "POST",
      body: { reason: reason || "revoked by the merchant on the principal's behalf" },
    });
    revalidatePath("/merchant/mandates");
    revalidatePath("/merchant/traffic");
    return { ok: true, message: "Mandate revoked. It is refused at its next use." };
  } catch (error) {
    return failure(error);
  }
}

export async function setKillSwitch(agentId: string, enabled: boolean): Promise<ActionResult> {
  try {
    await backendFetch(`/merchant/agents/${encodeURIComponent(agentId)}`, {
      method: "PATCH",
      body: { kill_switch: enabled },
    });
    revalidatePath("/merchant/agents");
    revalidatePath("/merchant/traffic");
    return {
      ok: true,
      message: enabled
        ? "Kill switch on. This agent is stopped immediately; others are unaffected."
        : "Kill switch off. This agent may transact again.",
    };
  } catch (error) {
    return failure(error);
  }
}

export async function updateAgentLimits(
  agentId: string,
  limits: {
    max_spend_per_window_minor?: number;
    max_transactions_per_window?: number;
    blocked_categories?: string[];
    allowed_categories?: string[];
  },
): Promise<ActionResult> {
  try {
    await backendFetch(`/merchant/agents/${encodeURIComponent(agentId)}`, {
      method: "PATCH",
      body: limits,
    });
    revalidatePath("/merchant/agents");
    return { ok: true, message: "Agent limits updated." };
  } catch (error) {
    return failure(error);
  }
}

export async function resolveException(exceptionId: string): Promise<ActionResult> {
  try {
    await backendFetch(`/merchant/exceptions/${encodeURIComponent(exceptionId)}/resolve`, {
      method: "POST",
    });
    revalidatePath("/merchant");
    return { ok: true, message: "Exception marked as reconciled." };
  } catch (error) {
    return failure(error);
  }
}

export async function openDispute(correlationId: string, claim: string): Promise<ActionResult> {
  try {
    const created = await backendFetch<{ id: string }>("/merchant/disputes", {
      method: "POST",
      body: {
        correlation_id: correlationId,
        claim: claim || "the cardholder states this purchase was not authorised",
      },
    });
    revalidatePath("/merchant/disputes");
    return { ok: true, message: `Representment assembled as dispute ${created.id}.` };
  } catch (error) {
    return failure(error);
  }
}

export async function decideDispute(
  disputeId: string,
  outcome: "contested" | "refunded",
): Promise<ActionResult> {
  try {
    await backendFetch(`/merchant/disputes/${disputeId}/decide`, {
      method: "POST",
      body: { outcome },
    });
    revalidatePath("/merchant/disputes");
    revalidatePath(`/merchant/disputes/${disputeId}`);
    return { ok: true, message: `Dispute recorded as ${outcome}.` };
  } catch (error) {
    return failure(error);
  }
}
