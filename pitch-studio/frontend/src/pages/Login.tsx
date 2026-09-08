import { FormEvent, useState } from "react";
import { api } from "../api";
import type { User } from "../types";

export default function Login({ onSignedIn }: { onSignedIn: (user: User) => void }) {
  const [email, setEmail] = useState("admin@example.com");
  const [password, setPassword] = useState("changeme");
  const [error, setError] = useState("");

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setError("");
    try {
      const user = (await api.login(email, password)) as User;
      onSignedIn(user);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sign in failed");
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
          />
        </label>
        {error && <p className="text-sm text-danger">{error}</p>}
        <button className="btn-accent w-full" type="submit">
          Sign in
        </button>
      </form>
    </div>
  );
}
