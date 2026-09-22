import { moduleName } from "../modules";
import type { DeckSlide } from "../types";

/**
 * A slide is a page of the brand deck, shown full-bleed. Nothing is laid out in
 * the browser, so the preview is exactly what the downloaded PPTX contains.
 */
export default function SlideView({
  slide,
  index = 1,
  total = 1,
  compact = false,
}: {
  slide: DeckSlide;
  index?: number;
  total?: number;
  compact?: boolean;
}) {
  const source = slide.image_url || (slide.page ? `/api/brand-deck/pages/${slide.page}.jpg` : "");

  if (!source) {
    return (
      <div className="flex h-full w-full items-center justify-center bg-offwhite px-[8%] text-center">
        <p className="text-[2cqw] text-grey">
          {slide.title || "This slide is not linked to a brand deck page."}
        </p>
      </div>
    );
  }

  const label = [moduleName(slide.module_id), slide.title].filter(Boolean).join(" — ");

  return (
    <div className="brand-slide relative h-full w-full bg-black">
      <img
        src={source}
        alt={label || `Brand deck page ${slide.page}`}
        className="h-full w-full object-contain"
        loading={compact ? "lazy" : "eager"}
        draggable={false}
      />
      {slide.source === "generated" && (
        <span className="absolute right-[1.4%] top-[2.2%] rounded-full bg-black/75 px-[1.1%] py-[0.45%] text-[1.15cqw] font-semibold tracking-wide text-white shadow-sm backdrop-blur-sm">
          Auto generated
        </span>
      )}
      {!compact && (
        <span className="sr-only">
          Slide {index} of {total}: {label}
        </span>
      )}
    </div>
  );
}
