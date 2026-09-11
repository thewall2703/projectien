import { AnimatePresence, motion } from "framer-motion";
import { useEffect, useRef, useState } from "react";
import type { DeckSlide } from "../types";
import SlideView from "./SlideView";

/**
 * Google Slides–style presenter: one big slide, side click/arrow nav,
 * keyboard when focused, thumbnail filmstrip underneath.
 */
export default function SlideStage({ slides }: { slides: DeckSlide[] }) {
  const [index, setIndex] = useState(0);
  const [focused, setFocused] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const filmRef = useRef<HTMLDivElement>(null);
  const total = slides.length;
  const current = slides[Math.min(index, Math.max(total - 1, 0))];

  useEffect(() => {
    setIndex(0);
  }, [slides]);

  useEffect(() => {
    if (!focused || total < 2) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "ArrowRight" || event.key === "PageDown" || event.key === " ") {
        event.preventDefault();
        setIndex((value) => Math.min(value + 1, total - 1));
      } else if (event.key === "ArrowLeft" || event.key === "PageUp") {
        event.preventDefault();
        setIndex((value) => Math.max(value - 1, 0));
      } else if (event.key === "Home") {
        event.preventDefault();
        setIndex(0);
      } else if (event.key === "End") {
        event.preventDefault();
        setIndex(total - 1);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [focused, total]);

  useEffect(() => {
    const node = filmRef.current?.querySelector<HTMLElement>(`[data-thumb="${index}"]`);
    node?.scrollIntoView({ behavior: "smooth", inline: "center", block: "nearest" });
  }, [index]);

  if (!total || !current) {
    return (
      <div className="glass-panel flex min-h-[40vh] items-center justify-center p-8 text-center text-grey">
        No slides in this deck yet.
      </div>
    );
  }

  const go = (next: number) => setIndex(Math.max(0, Math.min(next, total - 1)));
  const canPrev = index > 0;
  const canNext = index < total - 1;

  return (
    <div
      ref={rootRef}
      tabIndex={0}
      role="region"
      aria-label={`Slide deck, slide ${index + 1} of ${total}`}
      className="outline-none"
      onFocus={() => setFocused(true)}
      onBlur={(event) => {
        if (!rootRef.current?.contains(event.relatedTarget as Node)) setFocused(false);
      }}
      onMouseEnter={() => setFocused(true)}
    >
      <div className="stage relative aspect-[16/9] w-full overflow-hidden bg-black">
        <AnimatePresence mode="wait" initial={false}>
          <motion.div
            key={`${current.page ?? index}-${index}`}
            className="absolute inset-0"
            initial={{ opacity: 0, x: 24 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: -24 }}
            transition={{ duration: 0.22, ease: [0.22, 1, 0.36, 1] }}
          >
            <SlideView slide={current} index={index + 1} total={total} />
          </motion.div>
        </AnimatePresence>

        {total > 1 && (
          <>
            <button
              type="button"
              aria-label="Previous slide"
              disabled={!canPrev}
              className="absolute left-0 top-0 z-10 h-full w-[18%] cursor-w-resize bg-transparent disabled:cursor-default"
              onClick={() => go(index - 1)}
            />
            <button
              type="button"
              aria-label="Next slide"
              disabled={!canNext}
              className="absolute right-0 top-0 z-10 h-full w-[18%] cursor-e-resize bg-transparent disabled:cursor-default"
              onClick={() => go(index + 1)}
            />
            <button
              type="button"
              aria-label="Previous slide"
              disabled={!canPrev}
              className="absolute left-3 top-1/2 z-20 flex h-10 w-10 -translate-y-1/2 items-center justify-center rounded-full border border-white/20 bg-black/45 text-lg text-offwhite backdrop-blur-md transition hover:bg-black/65 disabled:opacity-30"
              onClick={() => go(index - 1)}
            >
              ‹
            </button>
            <button
              type="button"
              aria-label="Next slide"
              disabled={!canNext}
              className="absolute right-3 top-1/2 z-20 flex h-10 w-10 -translate-y-1/2 items-center justify-center rounded-full border border-white/20 bg-black/45 text-lg text-offwhite backdrop-blur-md transition hover:bg-black/65 disabled:opacity-30"
              onClick={() => go(index + 1)}
            >
              ›
            </button>
          </>
        )}

        <div className="pointer-events-none absolute bottom-3 right-3 z-20 rounded-full bg-black/55 px-3 py-1 text-xs font-medium text-offwhite backdrop-blur-md">
          {index + 1} / {total}
        </div>
      </div>

      {total > 1 && (
        <div
          ref={filmRef}
          className="mt-3 flex gap-2 overflow-x-auto pb-1"
          style={{ scrollbarWidth: "thin" }}
        >
          {slides.map((slide, thumbIndex) => {
            const active = thumbIndex === index;
            return (
              <button
                key={`${slide.page ?? "p"}-${thumbIndex}`}
                type="button"
                data-thumb={thumbIndex}
                aria-label={`Go to slide ${thumbIndex + 1}`}
                aria-current={active ? "true" : undefined}
                className={`relative h-16 w-28 shrink-0 overflow-hidden rounded-lg border transition ${
                  active
                    ? "border-black ring-2 ring-black/20"
                    : "border-black/10 opacity-70 hover:opacity-100"
                }`}
                onClick={() => go(thumbIndex)}
              >
                <SlideView slide={slide} index={thumbIndex + 1} total={total} compact />
                <span className="pointer-events-none absolute bottom-1 left-1 rounded bg-black/60 px-1.5 text-[10px] text-offwhite">
                  {thumbIndex + 1}
                </span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
