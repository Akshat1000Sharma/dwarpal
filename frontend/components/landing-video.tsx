"use client";

import { useRef, useState } from "react";

/**
 * The walkthrough.
 *
 * Three things matter here and they are all measurable. The aspect-ratio box reserves the space
 * before anything loads, so the video contributes nothing to cumulative layout shift, and the
 * ratio is chosen by a CSS breakpoint rather than by script so it is already correct on the first
 * paint. preload is metadata, so neither file is on the critical path and the largest contentful
 * paint stays the headline. And it never plays until asked: autoplaying video with sound is the
 * fastest way to make somebody close a tab.
 *
 * The landscape and portrait cuts are two files, not one file letterboxed twice. Both are in the
 * markup because the choice between them has to stay in CSS; only the visible one is ever played.
 *
 * Each cut carries its own poster for the same reason. The poster is what every visitor sees,
 * because the video never plays until asked, and a 16:9 still in the 9:16 box would sit in the
 * middle of two empty bands.
 */
export function LandingVideo({
  landscape,
  portrait,
  landscapePoster,
  portraitPoster,
}: {
  landscape: string;
  portrait: string;
  landscapePoster?: string;
  portraitPoster?: string;
}) {
  return (
    <>
      <div className="sm:hidden">
        <Player src={portrait} poster={portraitPoster} ratio="9 / 16" />
      </div>
      <div className="hidden sm:block">
        <Player src={landscape} poster={landscapePoster} ratio="16 / 9" />
      </div>
    </>
  );
}

function Player({ src, poster, ratio }: { src: string; poster?: string; ratio: string }) {
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
      <div className="relative bg-sunken" style={{ aspectRatio: ratio }}>
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
