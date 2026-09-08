import { useEffect, useMemo, useState } from "react";
import type { DeckSlide } from "../types";
import SlideView from "./SlideView";

export default function DeckPreview({ slides }: { slides: DeckSlide[] }) {
  const [current, setCurrent] = useState(0);
  const safeSlides = slides;
  const lastIndex = Math.max(0, safeSlides.length - 1);
  const index = Math.min(current, lastIndex);
  const slide = safeSlides[index];

  const sectionIndexes = useMemo(() => {
    let count = 0;
    return safeSlides.map((item) => {
      if (item.layout === "section") count += 1;
      return count || 1;
    });
  }, [safeSlides]);

  useEffect(() => {
    setCurrent((value) => Math.min(value, lastIndex));
  }, [lastIndex]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "ArrowRight") setCurrent((value) => Math.min(lastIndex, value + 1));
      if (event.key === "ArrowLeft") setCurrent((value) => Math.max(0, value - 1));
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [lastIndex]);

  if (!slide) {
    return <p className="text-sm text-muted">No slides yet.</p>;
  }

  return (
    <div className="space-y-4">
      <div className="stage">
        <SlideView
          slide={slide}
          index={index + 1}
          total={safeSlides.length}
          sectionIndex={sectionIndexes[index]}
        />
      </div>
      <div className="flex items-center justify-between">
        <button className="btn" type="button" onClick={() => setCurrent((value) => Math.max(0, value - 1))}>
          Previous
        </button>
        <p className="text-sm text-muted">
          Slide {index + 1} of {safeSlides.length}
        </p>
        <button
          className="btn"
          type="button"
          onClick={() => setCurrent((value) => Math.min(lastIndex, value + 1))}
        >
          Next
        </button>
      </div>
      <div className="flex gap-3 overflow-x-auto pb-2">
        {safeSlides.map((item, itemIndex) => (
          <button
            key={`${item.layout}-${item.title}-${itemIndex}`}
            type="button"
            onClick={() => setCurrent(itemIndex)}
            className={`w-40 shrink-0 overflow-hidden rounded-lg border ${
              itemIndex === index ? "border-accent shadow-card" : "border-line"
            }`}
          >
            <div className="pointer-events-none h-[90px] w-[356px] origin-top-left scale-[0.28]">
              <div className="h-[321px] w-[571px]">
                <SlideView
                  slide={item}
                  index={itemIndex + 1}
                  total={safeSlides.length}
                  sectionIndex={sectionIndexes[itemIndex]}
                  compact
                />
              </div>
            </div>
          </button>
        ))}
      </div>
    </div>
  );
}
