import type { ButtonHTMLAttributes, ReactNode } from "react";
import { Link } from "react-router-dom";

export function Wordmark({
  to = "/",
  className = "",
  size = "md",
}: {
  to?: string;
  className?: string;
  size?: "sm" | "md" | "lg";
}) {
  const sizeClass =
    size === "lg" ? "text-4xl md:text-5xl" : size === "sm" ? "text-lg" : "text-xl md:text-2xl";
  return (
    <Link to={to} className={`font-display leading-none tracking-tight text-black ${sizeClass} ${className}`}>
      <span className="text-brand-yellow">P</span>itch Studio
    </Link>
  );
}

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
    <div className="glass-panel px-6 py-10 text-center">
      <p className="font-medium text-black">{title}</p>
      {description && <p className="mt-2 text-sm text-grey">{description}</p>}
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

export function GoogleG({ className = "h-5 w-5" }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" aria-hidden>
      <path
        fill="#4285F4"
        d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"
      />
      <path
        fill="#34A853"
        d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"
      />
      <path
        fill="#FBBC05"
        d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z"
      />
      <path
        fill="#EA4335"
        d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"
      />
    </svg>
  );
}
