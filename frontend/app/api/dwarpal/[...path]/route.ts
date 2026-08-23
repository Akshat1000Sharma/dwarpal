import type { NextRequest } from "next/server";

import { backendOrigin } from "@/lib/backend";

/**
 * Same-origin proxy to the backend.
 *
 * Client components poll through this path rather than calling the backend directly, so the
 * browser never makes a cross-origin request and no CORS configuration is needed. Only the
 * merchant surface is reachable: the agent-facing endpoints are not proxied, because agents
 * transact against the backend directly and must not be able to reach it through the dashboard's
 * origin.
 */

const ALLOWED_PREFIXES = ["merchant/", "health"];

function permitted(path: string): boolean {
  return ALLOWED_PREFIXES.some((prefix) => path === prefix || path.startsWith(prefix));
}

async function forward(request: NextRequest, path: string[]): Promise<Response> {
  const joined = path.join("/");
  if (!permitted(joined)) {
    return Response.json(
      { error: "only the merchant surface is proxied through the dashboard origin" },
      { status: 404 },
    );
  }

  const search = request.nextUrl.search;
  const body = request.method === "GET" || request.method === "HEAD" ? undefined : await request.text();

  try {
    const upstream = await fetch(`${backendOrigin()}/${joined}${search}`, {
      method: request.method,
      headers: {
        Accept: "application/json",
        ...(body ? { "Content-Type": "application/json" } : {}),
      },
      body,
      cache: "no-store",
    });
    const text = await upstream.text();
    return new Response(text, {
      status: upstream.status,
      headers: { "Content-Type": upstream.headers.get("Content-Type") ?? "application/json" },
    });
  } catch (error) {
    return Response.json(
      { error: "the backend is not reachable", detail: String(error).slice(0, 200) },
      { status: 502 },
    );
  }
}

/**
 * params is a promise in Next 16. The shape is written out rather than using the generated
 * RouteContext helper, because that helper only exists once a build has emitted .next/types and
 * CI type-checks before it builds.
 */
type Context = { params: Promise<{ path: string[] }> };

export async function GET(request: NextRequest, context: Context) {
  const { path } = await context.params;
  return forward(request, path);
}

export async function POST(request: NextRequest, context: Context) {
  const { path } = await context.params;
  return forward(request, path);
}

export async function PATCH(request: NextRequest, context: Context) {
  const { path } = await context.params;
  return forward(request, path);
}
