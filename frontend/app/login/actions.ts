"use server";

import { cookies } from "next/headers";
import { redirect } from "next/navigation";

import { PERSONA_COOKIE, type Persona } from "./persona";

/**
 * Choosing a view, not signing in.
 *
 * There is deliberately no authentication here. Dwarpal's security boundary is the credential
 * chain an agent presents and the merchant token the dashboard sends server side; a password box
 * on this page would suggest a second boundary that does not exist. The cookie only decides which
 * sidebar you get.
 */
export async function chooseProfile(persona: Persona): Promise<void> {
  // A server action is a public endpoint and the Persona type is erased at runtime, so anything
  // that is not one of the two known values is treated as the default rather than stored.
  const chosen: Persona = persona === "buyer" ? "buyer" : "merchant";
  const jar = await cookies();
  jar.set(PERSONA_COOKIE, chosen, {
    httpOnly: false,
    sameSite: "lax",
    path: "/",
    maxAge: 60 * 60 * 24 * 30,
  });
  redirect(chosen === "buyer" ? "/buyer" : "/merchant");
}
