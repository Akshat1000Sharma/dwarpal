import "server-only";

/**
 * The single place the backend URL exists.
 *
 * Components never learn it. They call these helpers from the server, or reach the backend through
 * the same-origin proxy at /api/dwarpal. There are no cross-origin calls from the browser, so no
 * CORS configuration is needed anywhere.
 */

export class BackendError extends Error {
  constructor(
    readonly status: number,
    readonly path: string,
    message: string,
  ) {
    super(message);
    this.name = "BackendError";
  }
}

export function backendOrigin(): string {
  const origin = process.env.BACKEND_ORIGIN;
  if (!origin) {
    throw new Error(
      "BACKEND_ORIGIN is not set. Copy .env.example to .env.local and point it at the backend.",
    );
  }
  return origin.replace(/\/$/, "");
}

/** The merchant surface is guarded by a shared secret; the dashboard is the only caller. */
export function merchantToken(): Record<string, string> {
  const token = process.env.MERCHANT_API_TOKEN;
  return token ? { "X-Merchant-Token": token } : {};
}

type RequestOptions = {
  method?: string;
  body?: unknown;
  /** Live merchant data is never cached; a stale verdict log would be misleading. */
  revalidate?: number | false;
};

export async function backendFetch<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = "GET", body, revalidate = false } = options;

  const response = await fetch(`${backendOrigin()}${path}`, {
    method,
    headers: {
      Accept: "application/json",
      ...merchantToken(),
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: revalidate === false ? "no-store" : "force-cache",
    ...(revalidate === false ? {} : { next: { revalidate } }),
  });

  const text = await response.text();
  if (!response.ok) {
    throw new BackendError(response.status, path, text.slice(0, 400) || response.statusText);
  }
  return (text ? JSON.parse(text) : {}) as T;
}

/** Read a surface, returning a fallback rather than blanking the page when the backend is down. */
export async function backendRead<T>(path: string, fallback: T): Promise<T> {
  try {
    return await backendFetch<T>(path);
  } catch (error) {
    // The page still renders its empty state rather than blanking, but an empty table caused by a
    // 401 must not look identical to one caused by there being nothing to show.
    console.error(
      `[dwarpal] ${path} failed, rendering the fallback:`,
      error instanceof BackendError ? `${error.status} ${error.message}` : error,
    );
    return fallback;
  }
}

export async function backendReachable(): Promise<boolean> {
  try {
    await backendFetch<{ status: string }>("/health");
    return true;
  } catch {
    return false;
  }
}
