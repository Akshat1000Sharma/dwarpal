"use client";

import { useState, type ReactNode } from "react";

/** A copy button that says what it did. Used wherever the page hands over a value to paste. */
export function CopyButton({
  value,
  label = "Copy",
  className = "",
}: {
  value: string;
  label?: string;
  className?: string;
}) {
  const [copied, setCopied] = useState(false);

  return (
    <button
      type="button"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(value);
          setCopied(true);
          window.setTimeout(() => setCopied(false), 1600);
        } catch {
          // Clipboard access can be refused; the value is on screen either way.
          setCopied(false);
        }
      }}
      className={`shrink-0 rounded-[7px] border border-line px-2.5 py-1 text-[11px] font-medium text-muted transition-colors hover:bg-sunken hover:text-ink ${className}`}
      aria-live="polite"
    >
      {copied ? "Copied" : label}
    </button>
  );
}

/** A labelled value with a copy button, for tokens, endpoints and card details. */
export function CopyField({
  label,
  value,
  mono = true,
  hint,
}: {
  label: string;
  value: string;
  mono?: boolean;
  hint?: string;
}) {
  return (
    <div>
      <div className="mb-1.5 flex items-baseline justify-between gap-3">
        <span className="text-[11px] font-semibold uppercase tracking-[0.07em] text-faint">
          {label}
        </span>
        {hint && <span className="text-[11px] text-faint">{hint}</span>}
      </div>
      <div className="flex items-center gap-2 rounded-[9px] border border-line bg-sunken px-3 py-2">
        <span
          className={`min-w-0 flex-1 truncate text-[12px] text-ink ${mono ? "font-mono" : ""}`}
        >
          {value}
        </span>
        <CopyButton value={value} />
      </div>
    </div>
  );
}

/** A copyable block, for a curl command or a config file. */
export function CopyBlock({
  label,
  value,
  children,
}: {
  label: string;
  value: string;
  children?: ReactNode;
}) {
  return (
    <div>
      <div className="mb-1.5 flex items-center justify-between gap-3">
        <span className="text-[11px] font-semibold uppercase tracking-[0.07em] text-faint">
          {label}
        </span>
        <CopyButton value={value} />
      </div>
      {children}
      <pre className="scroll-x rounded-[9px] border border-line bg-sunken p-3 text-[11.5px] leading-relaxed text-ink">
        {value}
      </pre>
    </div>
  );
}
