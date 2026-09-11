import { FormEvent, useState } from "react";
import { api } from "../api";
import { Button, ErrorBanner, GoogleG, Wordmark } from "../components/ui";
import type { User } from "../types";

export default function Login({ onSignedIn }: { onSignedIn: (user: User) => void }) {
  const [email, setEmail] = useState("admin@example.com");
  const [password, setPassword] = useState("changeme");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [showFields, setShowFields] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      const user = (await api.login(email, password)) as User;
      onSignedIn(user);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sign in failed");
      setShowFields(true);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="app-canvas relative flex items-center justify-center px-6">
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0"
        style={{
          background:
            "radial-gradient(ellipse 70% 45% at 50% 0%, rgba(245,197,24,0.1), transparent 55%)",
        }}
      />
      <form onSubmit={submit} className="glass-panel relative w-full max-w-md space-y-6 p-8 md:p-10">
        <div className="text-center">
          <Wordmark to="/login" size="lg" className="pointer-events-none" />
          <p className="mt-3 text-sm text-grey">Sign in to generate a script, deck, and asset pack.</p>
        </div>

        {!showFields ? (
          <div className="space-y-4">
            <button
              type="submit"
              disabled={busy}
              className="flex w-full items-center justify-center gap-3 rounded-xl border border-black/10 bg-white px-4 py-3 text-sm font-medium text-black shadow-sm transition hover:bg-offwhite disabled:opacity-50"
            >
              {busy ? (
                <span className="spinner" />
              ) : (
                <>
                  <GoogleG />
                  Continue with Google
                </>
              )}
            </button>
            <p className="text-center text-xs text-grey">
              Uses your workspace account.{" "}
              <button
                type="button"
                className="underline hover:text-black"
                onClick={() => setShowFields(true)}
              >
                Sign in with email
              </button>
            </p>
          </div>
        ) : (
          <div className="space-y-4">
            <button
              type="submit"
              disabled={busy}
              className="flex w-full items-center justify-center gap-3 rounded-xl border border-black/10 bg-white px-4 py-3 text-sm font-medium text-black shadow-sm transition hover:bg-offwhite disabled:opacity-50"
            >
              {busy ? <span className="spinner" /> : <GoogleG />}
              {busy ? "Signing in…" : "Continue with Google"}
            </button>
            <div className="relative py-1 text-center text-xs text-grey">
              <span className="relative z-10 bg-transparent px-2">or</span>
              <span className="absolute left-0 top-1/2 h-px w-full -translate-y-1/2 bg-black/10" />
            </div>
            <label className="block text-sm text-grey-dark">
              Email
              <input
                className="field mt-1"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                type="email"
                required
                disabled={busy}
              />
            </label>
            <label className="block text-sm text-grey-dark">
              Password
              <input
                className="field mt-1"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                type="password"
                required
                disabled={busy}
              />
            </label>
            <Button variant="accent" className="w-full" type="submit" loading={busy}>
              Sign in
            </Button>
          </div>
        )}
        <ErrorBanner message={error} />
      </form>
    </div>
  );
}
