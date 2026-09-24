import { FormEvent, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { Button, ErrorBanner, Wordmark } from "../components/ui";
import type { User } from "../types";

declare global {
  interface Window {
    google?: {
      accounts: {
        id: {
          initialize: (config: {
            client_id: string;
            callback: (response: { credential: string }) => void;
            hd?: string;
            auto_select?: boolean;
          }) => void;
          renderButton: (
            parent: HTMLElement,
            options: {
              theme?: string;
              size?: string;
              width?: number;
              text?: string;
              shape?: string;
            },
          ) => void;
        };
      };
    };
  }
}

function loadGisScript(): Promise<void> {
  if (window.google?.accounts?.id) return Promise.resolve();
  const existing = document.querySelector<HTMLScriptElement>('script[data-gis="true"]');
  if (existing) {
    // Script may already have loaded (e.g. React Strict Mode remount). Waiting for
    // "load" again would hang forever because that event does not re-fire.
    if (existing.dataset.loaded === "true" || window.google?.accounts?.id) {
      return Promise.resolve();
    }
    return new Promise((resolve, reject) => {
      existing.addEventListener(
        "load",
        () => {
          existing.dataset.loaded = "true";
          resolve();
        },
        { once: true },
      );
      existing.addEventListener("error", () => reject(new Error("Failed to load Google sign-in")), {
        once: true,
      });
    });
  }
  return new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = "https://accounts.google.com/gsi/client";
    script.async = true;
    script.dataset.gis = "true";
    script.onload = () => {
      script.dataset.loaded = "true";
      resolve();
    };
    script.onerror = () => reject(new Error("Failed to load Google sign-in"));
    document.head.appendChild(script);
  });
}

export default function Login({ onSignedIn }: { onSignedIn: (user: User) => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [showFields, setShowFields] = useState(false);
  const [googleReady, setGoogleReady] = useState(false);
  const [googleConfigured, setGoogleConfigured] = useState(true);
  const [configMessage, setConfigMessage] = useState("");
  const buttonRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const config = await api.authConfig();
        const clientId = (config.google_client_id || "").trim();
        if (!clientId) {
          if (!cancelled) {
            setGoogleConfigured(false);
            setConfigMessage("Google sign-in is not configured. Use email sign-in, or set GOOGLE_CLIENT_ID.");
            setShowFields(true);
          }
          return;
        }
        await loadGisScript();
        if (cancelled || !window.google?.accounts?.id) return;
        window.google.accounts.id.initialize({
          client_id: clientId,
          hd: config.google_allowed_domain || "mastersunion.org",
          callback: async (response) => {
            setError("");
            setBusy(true);
            try {
              const user = (await api.googleLogin(response.credential)) as User;
              onSignedIn(user);
            } catch (err) {
              setError(err instanceof Error ? err.message : "Google sign-in failed");
              setShowFields(true);
            } finally {
              setBusy(false);
            }
          },
        });
        if (!cancelled) setGoogleReady(true);
      } catch (err) {
        if (!cancelled) {
          setGoogleConfigured(false);
          setConfigMessage(
            err instanceof Error ? err.message : "Could not load Google sign-in configuration.",
          );
          setShowFields(true);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [onSignedIn]);

  useEffect(() => {
    if (!googleReady || !buttonRef.current || !window.google?.accounts?.id) return;
    buttonRef.current.innerHTML = "";
    window.google.accounts.id.renderButton(buttonRef.current, {
      theme: "outline",
      size: "large",
      width: 320,
      text: "continue_with",
      shape: "rectangular",
    });
  }, [googleReady, showFields]);

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
            <div ref={buttonRef} className="flex min-h-[44px] justify-center" />
            {busy && (
              <p className="flex items-center justify-center gap-2 text-sm text-grey">
                <span className="spinner" />
                Signing in…
              </p>
            )}
            <p className="text-center text-xs text-grey">
              Uses your Masters&apos; Union Google account.{" "}
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
            {googleConfigured && (
              <>
                <div ref={buttonRef} className="flex min-h-[44px] justify-center" />
                <div className="relative py-1 text-center text-xs text-grey">
                  <span className="relative z-10 bg-transparent px-2">or</span>
                  <span className="absolute left-0 top-1/2 h-px w-full -translate-y-1/2 bg-black/10" />
                </div>
              </>
            )}
            {configMessage && <p className="text-sm text-grey">{configMessage}</p>}
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
            {googleConfigured && (
              <p className="text-center text-xs text-grey">
                <button
                  type="button"
                  className="underline hover:text-black"
                  onClick={() => setShowFields(false)}
                >
                  Back to Google sign-in
                </button>
              </p>
            )}
          </div>
        )}
        <ErrorBanner message={error} />
      </form>
    </div>
  );
}
