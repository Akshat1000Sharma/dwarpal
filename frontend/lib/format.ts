/** Display helpers. Money is always integer minor units on the wire. */

/**
 * One rendering of money for the whole console.
 *
 * The currency code rather than the symbol, because the backend's own `display` strings are built
 * as "INR 1,234.00" and are shown on the same screens. Two spellings of one amount look like two
 * different things.
 *
 * en-US rather than en-IN for the same reason: en-IN groups in lakhs, so 1243290 renders as
 * "12,43,290.00" while Python's ":," renders "1,243,290.00". The two agree below a lakh and
 * diverge above it, which is worse than disagreeing everywhere because it survives small tests.
 */
export function money(amount: number, currency = "INR"): string {
  return (
    new Intl.NumberFormat("en-US", {
      style: "currency",
      currency,
      currencyDisplay: "code",
      minimumFractionDigits: 2,
    })
      .format(amount / 100)
      // Intl separates the code from the number with U+00A0. The backend writes a plain space,
      // and the two strings sit on the same screens, so this one is normalised to match.
      .replace(/ /g, " ")
  );
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
