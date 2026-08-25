"use client";

import { useRef, useState } from "react";

/**
 * The walkthrough, in a browser frame.
 *
 * Three things matter here and they are all measurable. The aspect-ratio box reserves the space
 * before anything loads, so the video contributes nothing to cumulative layout shift. preload is
 * metadata, so the 2.6MB file is not on the critical path and the largest contentful paint stays
 * the headline. And it never plays until asked: autoplaying video with sound is the fastest way
 * to make somebody close a tab.
 */
export function LandingVideo({ src, poster }: { src: string; poster?: string }) {
  const video = useRef<HTMLVideoElement>(null);
  const [started, setStarted] = useState(false);

  const play = () => {
    setStarted(true);
    // The element only gets controls once it is playing, so the first frame stays a clean poster.
    requestAnimationFrame(() => {
      void video.current?.play();
    });
  };

  return (
    <div className="overflow-hidden rounded-[14px] border border-line bg-surface shadow-e3">
      <div className="flex items-center gap-2 border-b border-line bg-sunken px-4 py-2.5">
        <span className="h-2.5 w-2.5 rounded-full bg-[#e5e8ee]" aria-hidden="true" />
        <span className="h-2.5 w-2.5 rounded-full bg-[#e5e8ee]" aria-hidden="true" />
        <span className="h-2.5 w-2.5 rounded-full bg-[#e5e8ee]" aria-hidden="true" />
        <span className="ml-3 truncate font-mono text-[11px] text-faint">
          dwarpal - a purchase, end to end
        </span>
      </div>

      <div className="relative bg-[#0b1b33]" style={{ aspectRatio: "16 / 9" }}>
        <video
          ref={video}
          src={src}
          poster={poster}
          preload="metadata"
          muted
          playsInline
          controls={started}
          onEnded={() => setStarted(false)}
          className="h-full w-full object-contain"
        >
          <track kind="captions" />
          Your browser cannot play this video. The same walkthrough is written out below.
        </video>

        {!started && (
          <button
            type="button"
            onClick={play}
            className="absolute inset-0 grid place-items-center bg-[color:var(--overlay)] transition-colors duration-300 hover:bg-[rgba(11,27,51,0.42)]"
            aria-label="Play the walkthrough"
          >
            <span className="flex items-center gap-3 rounded-full bg-white/95 px-5 py-3 shadow-e2">
              <svg viewBox="0 0 16 16" className="h-4 w-4 text-brand" aria-hidden="true">
                <path d="M4 2.5v11l9-5.5z" fill="currentColor" />
              </svg>
              <span className="text-[13px] font-medium text-ink">Watch it run</span>
            </span>
          </button>
        )}
      </div>
    </div>
  );
}
