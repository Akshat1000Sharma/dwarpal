"use client";

import { useEffect, useRef, type ReactNode } from "react";

/**
 * Scroll reveal, done cheaply.
 *
 * Transform and opacity only, so it stays on the compositor and holds 60fps on a phone. The
 * armed class is applied by script rather than in the markup, so a page with JavaScript disabled
 * or a crawler reading the HTML sees the content rather than a blank column. Under
 * prefers-reduced-motion the CSS neutralises both classes and nothing moves.
 */
export function Reveal({
  children,
  delay = 0,
  className = "",
}: {
  children: ReactNode;
  delay?: number;
  className?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const node = ref.current;
    if (!node) return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

    node.classList.add("reveal-armed");
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (!entry.isIntersecting) continue;
          const target = entry.target as HTMLElement;
          target.style.transitionDelay = `${delay}ms`;
          target.classList.add("reveal-in");
          target.classList.remove("reveal-armed");
          observer.unobserve(target);
        }
      },
      { rootMargin: "0px 0px -12% 0px", threshold: 0.05 },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [delay]);

  return (
    <div ref={ref} className={`reveal ${className}`}>
      {children}
    </div>
  );
}
