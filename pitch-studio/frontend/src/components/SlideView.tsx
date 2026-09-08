import { moduleName } from "../modules";
import type { DeckSlide } from "../types";

function Footer({
  compact,
  index,
  total,
  moduleId,
}: {
  compact?: boolean;
  index: number;
  total: number;
  moduleId?: string;
}) {
  if (compact) return null;
  const name = moduleName(moduleId);
  return (
    <div className="absolute inset-x-[6%] bottom-[4.2%] flex items-center justify-between border-t border-line pt-[1.1%] text-[1.35cqw] font-medium uppercase tracking-[0.16em] text-muted">
      <span>Masters' Union</span>
      <span>{name}</span>
      <span>
        {String(index).padStart(2, "0")} / {String(total).padStart(2, "0")}
      </span>
    </div>
  );
}

function Kicker({ children }: { children: string }) {
  return (
    <p className="text-[1.4cqw] font-semibold uppercase tracking-[0.22em] text-accent">{children}</p>
  );
}

export default function SlideView({
  slide,
  index = 1,
  total = 1,
  sectionIndex = 1,
  compact = false,
}: {
  slide: DeckSlide;
  index?: number;
  total?: number;
  sectionIndex?: number;
  compact?: boolean;
}) {
  const layout = slide.layout || "bullets";
  const kicker = moduleName(slide.module_id);
  const title = slide.title || kicker || "Masters' Union";

  if (layout === "title") {
    return (
      <div className="brand-slide relative h-full w-full bg-surface px-[6.8%] py-[8%]">
        <div className="absolute inset-x-0 top-0 h-[2.4%] bg-accent" />
        <div className="flex h-full flex-col justify-center">
          <Kicker>Masters' Union</Kicker>
          <h2 className="mt-[3%] max-w-[18em] font-display text-[5.4cqw] font-semibold leading-[1.15] text-ink">
            {title}
          </h2>
          <div className="mt-[3.2%] h-[0.7cqw] w-[16%] bg-gold" />
          {slide.subtitle && (
            <p className="mt-[2.4%] max-w-[32em] text-[2.2cqw] leading-snug text-muted">{slide.subtitle}</p>
          )}
        </div>
      </div>
    );
  }

  if (layout === "section") {
    return (
      <div className="brand-slide relative h-full w-full bg-paper px-[6.8%] py-[10%]">
        <p className="font-display text-[14cqw] font-semibold leading-none text-line">
          {String(sectionIndex).padStart(2, "0")}
        </p>
        <h2 className="mt-[1%] font-display text-[4.6cqw] font-semibold leading-tight text-accent-dark">
          {title}
        </h2>
        {slide.subtitle && <p className="mt-[2%] text-[2.1cqw] text-muted">{slide.subtitle}</p>}
      </div>
    );
  }

  if (layout === "cta") {
    return (
      <div className="brand-slide relative h-full w-full bg-accent px-[6.8%] py-[10%] text-white">
        <p className="text-[1.4cqw] font-semibold uppercase tracking-[0.22em] text-gold">Next step</p>
        <h2 className="mt-[3%] max-w-[16em] font-display text-[5cqw] font-semibold leading-tight">
          {title || "Next step"}
        </h2>
        {slide.subtitle && <p className="mt-[3%] text-[2.3cqw] text-white/80">{slide.subtitle}</p>}
      </div>
    );
  }

  if (layout === "stat_pair") {
    const stats = (slide.stats || []).slice(0, 4);
    return (
      <div className="brand-slide relative h-full w-full bg-surface px-[6%] pb-[10%] pt-[5.5%]">
        <Kicker>{kicker || "Outcomes"}</Kicker>
        <h2 className="mt-[1.2%] font-display text-[3.6cqw] font-semibold leading-tight">{title}</h2>
        <div className="mt-[4%] grid grid-cols-2 gap-[2.2%]">
          {stats.map((stat, statIndex) => (
            <div key={`${stat.label}-${statIndex}`} className="rounded-[1.2cqw] bg-paper px-[4%] py-[6%]">
              <p className="font-display text-[4.6cqw] font-semibold leading-none text-accent">{stat.value}</p>
              <p className="mt-[8%] text-[1.5cqw] uppercase tracking-[0.12em] text-muted">{stat.label}</p>
            </div>
          ))}
        </div>
        <Footer compact={compact} index={index} total={total} moduleId={slide.module_id} />
      </div>
    );
  }

  if (layout === "quote") {
    return (
      <div className="brand-slide relative h-full w-full bg-surface px-[6%] py-[10%]">
        <div className="flex h-full items-center gap-[3.2%]">
          <div className="h-[48%] w-[0.9%] shrink-0 bg-accent" />
          <div className="max-w-[22em]">
            <p className="font-display text-[3.6cqw] font-semibold leading-snug">
              “{slide.quote || slide.title}”
            </p>
            {slide.attribution && <p className="mt-[4%] text-[1.85cqw] text-muted">{slide.attribution}</p>}
          </div>
        </div>
        <Footer compact={compact} index={index} total={total} moduleId={slide.module_id} />
      </div>
    );
  }

  if (layout === "agenda") {
    const items = (slide.bullets || []).slice(0, 8);
    return (
      <div className="brand-slide relative h-full w-full bg-surface px-[6%] pb-[10%] pt-[5.5%]">
        <Kicker>Agenda</Kicker>
        <h2 className="mt-[1.2%] font-display text-[3.6cqw] font-semibold">{title || "What we will cover"}</h2>
        <ol className="mt-[4%] space-y-[2.4%]">
          {items.map((item, itemIndex) => (
            <li key={item} className="flex items-baseline gap-[2.4%] text-[2.2cqw]">
              <span className="font-semibold text-accent">{String(itemIndex + 1).padStart(2, "0")}</span>
              <span className="font-display">{item}</span>
            </li>
          ))}
        </ol>
        <Footer compact={compact} index={index} total={total} moduleId={slide.module_id} />
      </div>
    );
  }

  if (layout === "profile") {
    const items = (slide.bullets || []).slice(0, 6);
    return (
      <div className="brand-slide relative h-full w-full bg-surface px-[6%] pb-[10%] pt-[5.5%]">
        <Kicker>{kicker || "People"}</Kicker>
        <h2 className="mt-[1.2%] font-display text-[3.6cqw] font-semibold">{title}</h2>
        {slide.subtitle && <p className="mt-[1%] text-[1.85cqw] text-accent">{slide.subtitle}</p>}
        <div className="mt-[3.5%] grid grid-cols-2 gap-[2%]">
          {items.map((item) => (
            <div key={item} className="rounded-[1.1cqw] bg-paper px-[4%] py-[5%] text-[1.85cqw] leading-snug">
              {item}
            </div>
          ))}
        </div>
        <Footer compact={compact} index={index} total={total} moduleId={slide.module_id} />
      </div>
    );
  }

  const bullets = (slide.bullets || []).slice(0, layout === "inventory" ? 8 : 6);
  const mid = layout === "inventory" ? Math.ceil(bullets.length / 2) : bullets.length;
  const columns = layout === "inventory" ? [bullets.slice(0, mid), bullets.slice(mid)] : [bullets];

  return (
    <div className="brand-slide relative h-full w-full bg-surface px-[6%] pb-[10%] pt-[5.5%]">
      <Kicker>{kicker || "Key points"}</Kicker>
      <h2 className="mt-[1.2%] font-display text-[3.6cqw] font-semibold leading-tight">{title}</h2>
      <div className={`mt-[3.6%] grid gap-[4%] ${columns.length > 1 ? "grid-cols-2" : "grid-cols-1"}`}>
        {columns.map((column, columnIndex) => (
          <ul key={columnIndex} className="space-y-[2.2%] text-[2cqw] leading-[1.45] text-ink">
            {column.map((item) => (
              <li key={item}>— {item}</li>
            ))}
          </ul>
        ))}
      </div>
      <Footer compact={compact} index={index} total={total} moduleId={slide.module_id} />
    </div>
  );
}
