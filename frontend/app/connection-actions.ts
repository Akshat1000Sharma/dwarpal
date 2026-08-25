"use server";

import { revalidatePath } from "next/cache";

import { backendFetch } from "@/lib/backend";
import type { Connection } from "@/lib/types";

export type ConnectionResult =
  | { ok: true; connection: Connection }
  | { ok: false; message: string };

function failed(error: unknown): ConnectionResult {
  const raw = String(error instanceof Error ? error.message : error);
  // The backend answers a bad phone number with a 422 and a JSON detail; show that rather than
  // the whole body, which is noise to somebody who mistyped a number.
  try {
    const parsed = JSON.parse(raw.replace(/^\d+\s*/, "")) as { detail?: string };
    if (parsed.detail) return { ok: false, message: parsed.detail };
  } catch {
    // Not JSON. Fall through.
  }
  const detail = raw.match(/"detail":"([^"]+)"/);
  return { ok: false, message: (detail ? detail[1] : raw).slice(0, 300) };
}

export async function createConnection(formData: FormData): Promise<ConnectionResult> {
  try {
    const connection = await backendFetch<Connection>("/merchant/connections", {
      method: "POST",
      body: {
        label: String(formData.get("label") ?? "").trim(),
        scope: String(formData.get("scope") ?? "buyer"),
        whatsapp: String(formData.get("whatsapp") ?? "").trim() || null,
      },
    });
    revalidatePath("/merchant/connections");
    revalidatePath("/buyer/setup");
    return { ok: true, connection };
  } catch (error) {
    return failed(error);
  }
}

export async function revokeConnection(connectionId: string): Promise<ConnectionResult> {
  try {
    const connection = await backendFetch<Connection>(
      `/merchant/connections/${encodeURIComponent(connectionId)}/revoke`,
      { method: "POST" },
    );
    revalidatePath("/merchant/connections");
    return { ok: true, connection };
  } catch (error) {
    return failed(error);
  }
}
