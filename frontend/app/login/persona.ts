/**
 * Which console the visitor picked.
 *
 * This lives apart from the server action because a "use server" module may only export async
 * functions, and both the constant and the type are needed by ordinary components.
 */

export const PERSONA_COOKIE = "dwarpal_persona";

export type Persona = "merchant" | "buyer";
