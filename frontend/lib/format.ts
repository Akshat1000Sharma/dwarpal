/** Display helpers. Money is always integer minor units on the wire. */

export function money(amount: number, currency = "INR"): string {
  return new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency,
    minimumFractionDigits: 2,
  }).format(amount / 100);
}

export function timestamp(value: string | null | undefined): string {
  if (!value) return "-";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toISOString().replace("T", " ").replace(/\.\d+Z$/, "Z");
}

export function relative(value: string | null | undefined): string {
  if (!value) return "-";
  const parsed = new Date(value).getTime();
  if (Number.isNaN(parsed)) return value;
  const seconds = Math.round((Date.now() - parsed) / 1000);
  if (seconds < 0) return `in ${describe(-seconds)}`;
  if (seconds < 5) return "just now";
  return `${describe(seconds)} ago`;
}

function describe(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86400)}d`;
}

export function percent(value: number): string {
  return `${(value * 100).toFixed(1)}%`;
}

/** Reason codes are SCREAMING_SNAKE on the wire; make them readable without losing the code. */
export function humanise(code: string): string {
  return code
    .toLowerCase()
    .split("_")
    .join(" ")
    .replace(/^\w/, (c) => c.toUpperCase());
}

export function shorten(value: string, head = 10, tail = 6): string {
  if (value.length <= head + tail + 3) return value;
  return `${value.slice(0, head)}...${value.slice(-tail)}`;
}
