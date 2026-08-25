"use client";

import Image from "next/image";
import { useState } from "react";

/**
 * A catalog item's picture, with a placeholder that is a design rather than a gap.
 *
 * There are three ways to end up without a photograph: the seed has no URL for the item, the host
 * is unreachable, or the file has moved. All three land on the same placeholder, so the grid never
 * shows an empty box. The placeholder is built from what the item already carries, a category glyph
 * over a tint keyed to the category, so it looks deliberate next to the items that do have a photo.
 */

export type ProductImageProps = {
  src?: string | null;
  alt?: string | null;
  title: string;
  category: string;
  /** Rendered above the fold on the first screen of the catalog. */
  priority?: boolean;
  className?: string;
  sizes?: string;
};

const TINTS: Record<string, { bg: string; ink: string }> = {
  grocery: { bg: "#eef6ee", ink: "#3f7a52" },
  alcohol: { bg: "#f7eef1", ink: "#8a3d55" },
  electronics: { bg: "#eef1f8", ink: "#41537f" },
  home: { bg: "#fbf3e9", ink: "#8a6234" },
  stationery: { bg: "#eef4f7", ink: "#3f6b7d" },
  "restricted-blades": { bg: "#f5f1f7", ink: "#6a4d84" },
};

const NEUTRAL = { bg: "#f1f3f7", ink: "#6b7688" };

const GLYPHS: Record<string, string> = {
  grocery: "M7 3h10l-1.2 4.5A5 5 0 0 1 12 21a5 5 0 0 1-3.8-13.5z M9.5 3v4M14.5 3v4",
  alcohol: "M8 3h8l-1 6a4 4 0 0 1-3 3v6M9 21h6M9 12a4 4 0 0 1-1-3l1-6",
  electronics: "M4 13a8 8 0 0 1 16 0M4 13v4a2 2 0 0 0 2 2h1v-6H6a2 2 0 0 0-2 2zM20 13v4a2 2 0 0 1-2 2h-1v-6h1a2 2 0 0 1 2 2z",
  home: "M12 3v3M7.5 8h9l2 6h-13zM12 14v5M9 19h6",
  stationery: "M6 3h9l3 3v15H6zM15 3v3h3M9 11h6M9 15h6",
  "restricted-blades": "M3 15l12-11 3 3-9 10zM3 15l4 4M6 19h12",
};

const FALLBACK_GLYPH = "M4 6h16v12H4zM4 14l4-4 4 4 3-3 5 5";

export function ProductImage({
  src,
  alt,
  title,
  category,
  priority = false,
  className = "",
  sizes = "(max-width: 640px) 100vw, (max-width: 1280px) 50vw, 33vw",
}: ProductImageProps) {
  // The src that failed, not a boolean: React can reuse this instance for a different item, and a
  // sticky flag would show the placeholder over a perfectly good photograph.
  const [failed, setFailed] = useState<string | null>(null);
  const showPhoto = Boolean(src) && failed !== src;

  return (
    <div className={`relative overflow-hidden bg-sunken ${className}`}>
      {showPhoto ? (
        <Image
          src={src as string}
          alt={alt || title}
          fill
          sizes={sizes}
          priority={priority}
          onError={() => setFailed(src as string)}
          className="object-cover"
        />
      ) : (
        <Placeholder title={title} category={category} />
      )}
    </div>
  );
}

function Placeholder({ title, category }: { title: string; category: string }) {
  const tint = TINTS[category] ?? NEUTRAL;
  const glyph = GLYPHS[category] ?? FALLBACK_GLYPH;
  const initials = title
    .split(/\s+/)
    .filter((word) => /[a-z0-9]/i.test(word))
    .slice(0, 2)
    .map((word) => word[0]?.toUpperCase() ?? "")
    .join("");

  return (
    <div
      className="absolute inset-0 grid place-items-center"
      style={{ backgroundColor: tint.bg }}
      role="img"
      aria-label={`${title}, no photograph available`}
    >
      <div className="flex flex-col items-center gap-2" style={{ color: tint.ink }}>
        <svg viewBox="0 0 24 24" className="h-9 w-9 opacity-70" aria-hidden="true">
          <path
            d={glyph}
            fill="none"
            stroke="currentColor"
            strokeWidth="1.4"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
        <span className="font-mono text-[11px] font-medium tracking-[0.14em] opacity-60">
          {initials || "--"}
        </span>
      </div>
    </div>
  );
}
