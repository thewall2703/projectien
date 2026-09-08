import { useEffect, useState } from "react";
import { moduleName } from "../modules";
import type { DeckSlide } from "../types";
import SlideView from "./SlideView";

export default function DeckPreview({ slides }: { slides: DeckSlide[] }) {
  const [current, setCurrent] = useState(0);
  const safeSlides = slides;
  const lastIndex = Math.max(0, safeSlides.length - 1);
  const index = Math.min(current, lastIndex);
  const slide = safeSlides[index];

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
        <SlideView slide={slide} index={index + 1} total={safeSlides.length} />
      </div>
      <div className="flex items-center justify-between gap-4">
        <button className="btn" type="button" onClick={() => setCurrent((value) => Math.max(0, value - 1))}>
          Previous
        </button>
        <div className="min-w-0 text-center">
          <p className="truncate text-sm font-medium text-ink">{slide.title}</p>
          <p className="text-xs text-muted">
            Slide {index + 1} of {safeSlides.length}
            {slide.module_id ? ` · ${moduleName(slide.module_id)}` : ""}
            {slide.page ? ` · brand deck p${slide.page}` : ""}
          </p>
        </div>
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
            key={`${item.page ?? item.layout}-${itemIndex}`}
            type="button"
            onClick={() => setCurrent(itemIndex)}
            title={item.title}
            className={`aspect-[16/9] w-40 shrink-0 overflow-hidden rounded-lg border ${
              itemIndex === index ? "border-accent shadow-card" : "border-line"
            }`}
          >
            <SlideView slide={item} index={itemIndex + 1} total={safeSlides.length} compact />
          </button>
        ))}
      </div>
    </div>
  );
}
