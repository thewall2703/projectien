import { FormEvent, useState } from "react";
import { api } from "../api";
import { Button, ErrorBanner } from "../components/ui";
import type { User } from "../types";

export default function Login({ onSignedIn }: { onSignedIn: (user: User) => void }) {
  const [email, setEmail] = useState("admin@example.com");
  const [password, setPassword] = useState("changeme");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      const user = (await api.login(email, password)) as User;
      onSignedIn(user);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sign in failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center px-6">
      <form onSubmit={submit} className="card w-full max-w-md space-y-5 p-8">
        <div>
          <p className="kicker">Masters' Union</p>
          <h1 className="mt-2 font-display text-4xl">Pitch Studio</h1>
          <p className="mt-2 text-sm text-muted">Sign in to generate a script, deck, and asset pack.</p>
        </div>
        <label className="block text-sm">
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
        <label className="block text-sm">
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
        <ErrorBanner message={error} />
        <Button variant="accent" className="w-full" type="submit" loading={busy}>
          Sign in
        </Button>
      </form>
    </div>
  );
}
