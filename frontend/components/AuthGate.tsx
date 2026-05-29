"use client";

import { useEffect, useState } from "react";
import {
  AuthProviders,
  AuthUser,
  getMe,
  getProviders,
  login,
  logout,
  oauthStartUrl,
  phoneRequest,
  phoneVerify,
} from "@/lib/api";

// Wraps the app: until the session resolves to a user, the lab is not mounted
// (so it never fires authenticated requests while logged out).
export function AuthGate({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<AuthUser | null | undefined>(undefined); // undefined = loading
  const [providers, setProviders] = useState<AuthProviders>({ local: true });

  useEffect(() => {
    getMe().then(setUser).catch(() => setUser(null));
    getProviders().then(setProviders).catch(() => {});
  }, []);

  if (user === undefined) return <div className="auth-loading">connecting…</div>;
  if (!user) return <Login providers={providers} onAuthed={() => getMe().then(setUser)} />;

  return (
    <>
      <div className="auth-bar">
        <span className="auth-who">
          {user.display_name || user.email || "user"} · <em>{user.role}</em>
        </span>
        <button
          className="auth-signout"
          onClick={async () => {
            await logout();
            setUser(null);
          }}
        >
          sign out
        </button>
      </div>
      {children}
    </>
  );
}

function Login({ providers, onAuthed }: { providers: AuthProviders; onAuthed: () => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [phone, setPhone] = useState("");
  const [code, setCode] = useState("");
  const [codeSent, setCodeSent] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function run(fn: () => Promise<unknown>) {
    setErr(null);
    try {
      await fn();
    } catch (e) {
      setErr(String((e as Error).message || e));
    }
  }

  return (
    <main className="auth-screen">
      <div className="auth-card">
        <h1>Aletheia</h1>
        <p className="auth-sub">sign in to your lights-out lab</p>

        <form
          className="auth-form"
          onSubmit={(e) => {
            e.preventDefault();
            run(() => login(email, password).then(onAuthed));
          }}
        >
          <input
            type="email" placeholder="email" value={email} autoComplete="username"
            onChange={(e) => setEmail(e.target.value)}
          />
          <input
            type="password" placeholder="password" value={password} autoComplete="current-password"
            onChange={(e) => setPassword(e.target.value)}
          />
          <button type="submit">Sign in</button>
        </form>

        {(providers.github || providers.feishu) && (
          <div className="auth-oauth">
            <div className="auth-divider">or</div>
            {providers.github && (
              <a className="auth-provider gh" href={oauthStartUrl("github")}>
                Continue with GitHub
              </a>
            )}
            {providers.feishu && (
              <a className="auth-provider fs" href={oauthStartUrl("feishu")}>
                Continue with Feishu
              </a>
            )}
          </div>
        )}

        {providers.phone && (
          <div className="auth-phone">
            <div className="auth-divider">phone</div>
            <div className="auth-phone-row">
              <input
                type="tel" placeholder="+66…" value={phone}
                onChange={(e) => setPhone(e.target.value)}
              />
              <button
                type="button"
                onClick={() => run(() => phoneRequest(phone).then(() => setCodeSent(true)))}
              >
                {codeSent ? "resend" : "send code"}
              </button>
            </div>
            {codeSent && (
              <div className="auth-phone-row">
                <input
                  type="text" placeholder="6-digit code" value={code}
                  onChange={(e) => setCode(e.target.value)}
                />
                <button
                  type="button"
                  onClick={() => run(() => phoneVerify(phone, code).then(onAuthed))}
                >
                  Verify
                </button>
              </div>
            )}
          </div>
        )}

        {err && <div className="auth-err">{err}</div>}
      </div>
    </main>
  );
}
