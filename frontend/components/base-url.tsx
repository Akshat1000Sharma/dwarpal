"use client";

import { createContext, useContext, useState, type ReactNode } from "react";

import { BASE_TOKEN, DEFAULT_BASE } from "@/lib/base-url";

import { CopyBlock, CopyField } from "./copy";

type BaseUrlState = { base: string; setBase: (value: string) => void };

const BaseUrlContext = createContext<BaseUrlState>({ base: DEFAULT_BASE, setBase: () => {} });

export function BaseUrlProvider({ children }: { children: ReactNode }) {
  const [base, setBase] = useState(DEFAULT_BASE);
  return <BaseUrlContext.Provider value={{ base, setBase }}>{children}</BaseUrlContext.Provider>;
}

export function useBaseUrl(): BaseUrlState {
  return useContext(BaseUrlContext);
}

/** A trailing slash would double up against every path the examples append. */
function normalise(value: string): string {
  return value.trim().replace(/\/+$/, "");
}

function applyBase(value: string, base: string): string {
  return value.replaceAll(BASE_TOKEN, base);
}

/**
 * The form that replaces the placeholder.
 *
 * It holds its own draft so typing does not rewrite the examples on every keystroke; the page
 * changes when Apply is pressed, which is what makes the effect legible.
 */
export function BaseUrlForm() {
  const { base, setBase } = useBaseUrl();
  const [draft, setDraft] = useState(base);

  const apply = () => {
    const next = normalise(draft);
    if (next) {
      setBase(next);
      setDraft(next);
    }
  };

  const isDefault = base === DEFAULT_BASE;

  return (
    <form
      className="space-y-3"
      onSubmit={(event) => {
        event.preventDefault();
        apply();
      }}
    >
      <div className="flex flex-col gap-2 sm:flex-row">
        <label className="min-w-0 flex-1">
          <span className="sr-only">Your public base URL</span>
          <input
            type="url"
            inputMode="url"
            spellCheck={false}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder={DEFAULT_BASE}
            className="w-full rounded-[9px] border border-line bg-surface px-3 py-2 font-mono text-[12.5px] text-ink placeholder:text-faint focus:border-brand focus:outline-none"
          />
        </label>
        <button
          type="submit"
          className="shrink-0 rounded-[9px] bg-brand px-4 py-2 text-[13px] font-medium text-white transition-colors hover:bg-brand-strong"
        >
          Apply
        </button>
      </div>

      <div className="flex items-center gap-3 text-[12px] text-muted">
        <span>
          Every example below is written against{" "}
          <span className="font-mono text-[11.5px] text-ink">{base}</span>.
        </span>
        {!isDefault && (
          <button
            type="button"
            onClick={() => {
              setBase(DEFAULT_BASE);
              setDraft(DEFAULT_BASE);
            }}
            className="shrink-0 text-brand transition-colors hover:underline"
          >
            Reset
          </button>
        )}
      </div>
    </form>
  );
}

/**
 * The two copy components, with the token substituted first.
 *
 * The substituted string is what reaches CopyField and CopyBlock, so the copy button hands over
 * the applied URL rather than the token: a command that has to be edited after pasting would
 * defeat the point of the button.
 */
export function CopyFieldBase(props: Parameters<typeof CopyField>[0]) {
  const { base } = useBaseUrl();
  return <CopyField {...props} value={applyBase(props.value, base)} />;
}

export function CopyBlockBase(props: Parameters<typeof CopyBlock>[0]) {
  const { base } = useBaseUrl();
  return <CopyBlock {...props} value={applyBase(props.value, base)} />;
}
