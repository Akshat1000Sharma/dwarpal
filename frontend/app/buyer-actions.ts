"use server";

import { redirect } from "next/navigation";

import { backendFetch } from "@/lib/backend";

/**
 * The buyer console's write actions.
 *
 * They run on the server and go through the same server-only module every read uses, so the
 * backend URL and the merchant token still exist in exactly one place and never reach the browser.
 */

export async function startRun(formData: FormData): Promise<void> {
  const prompt = String(formData.get("prompt") ?? "").trim();
  if (!prompt) return;

  const budget = Number(formData.get("budget") ?? 0);
  const constraints = formData
    .getAll("constraint")
    .map((value) => String(value).trim())
    .filter(Boolean);

  const created = await backendFetch<{ run_id: string }>("/buyer/runs", {
    method: "POST",
    body: {
      prompt,
      // The form collects rupees because that is what a person types; the wire is always paise.
      budget_cap_minor: Number.isFinite(budget) && budget > 0 ? Math.round(budget * 100) : null,
      natural_language: constraints,
    },
  });

  redirect(`/buyer/runs/${created.run_id}`);
}
