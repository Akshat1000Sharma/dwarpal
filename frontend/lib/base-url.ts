/**
 * The base URL the buyer setup page writes its examples against.
 *
 * These live outside the client component that uses them because the setup page is a server
 * component and has to embed the token in the example strings it renders. A server component
 * importing a value from a "use client" module receives a client reference proxy rather than the
 * value, so the token would stringify as a function that throws rather than as the placeholder.
 * A plain module is importable from both sides.
 */

/** Written into example strings on the server, substituted in the browser. */
export const BASE_TOKEN = "{{BASE}}";

/**
 * What the page ships with.
 *
 * A placeholder rather than any real tunnel: a free ngrok URL changes on every restart, and a
 * stale address in the page is worse than an obvious blank to fill in.
 */
export const DEFAULT_BASE = "https://your-tunnel.ngrok-free.dev";
