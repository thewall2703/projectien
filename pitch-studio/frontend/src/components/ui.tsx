import type { ButtonHTMLAttributes, ReactNode } from "react";

export function Spinner({ className = "" }: { className?: string }) {
  return <span className={`spinner ${className}`} aria-hidden />;
}

type ButtonVariant = "default" | "accent" | "ghost";

export function Button({
  loading = false,
  variant = "default",
  children,
  disabled,
  className = "",
  type = "button",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  loading?: boolean;
  variant?: ButtonVariant;
}) {
  const variantClass = variant === "accent" ? "btn-accent" : variant === "ghost" ? "btn-ghost" : "btn";
  return (
    <button
      type={type}
      className={`${variantClass} ${className}`}
      disabled={disabled || loading}
      {...props}
    >
      {loading && <Spinner />}
      {children}
    </button>
  );
}

function badgeTone(status: string) {
  if (status === "indexed") return "chip-active";
  if (status === "processing") return "chip-info";
  if (status === "done" || status === "ready") return status === "ready" ? "chip-info" : "chip-success";
  if (status === "stale") return "chip-warn";
  if (status === "failed") return "chip-danger";
  return "chip";
}

export function StatusBadge({
  status,
  className = "",
}: {
  status: string;
  className?: string;
}) {
  return <span className={`chip ${badgeTone(status)} ${className}`.trim()}>{status.replaceAll("_", " ")}</span>;
}

export function Skeleton({ className = "" }: { className?: string }) {
  return <div className={`skeleton ${className}`} />;
}

export function EmptyState({
  title,
  description,
  action,
}: {
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <div className="rounded-2xl border border-dashed border-line px-6 py-10 text-center">
      <p className="font-medium">{title}</p>
      {description && <p className="mt-2 text-sm text-muted">{description}</p>}
      {action && <div className="mt-4 flex justify-center">{action}</div>}
    </div>
  );
}

export function ErrorBanner({ message }: { message: string }) {
  if (!message) return null;
  return (
    <div className="rounded-xl border border-danger/20 bg-danger/5 px-4 py-3 text-sm text-danger" role="alert">
      {message}
    </div>
  );
}
