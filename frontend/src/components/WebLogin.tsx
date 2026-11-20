import { Eye, EyeOff, KeyRound, Loader2 } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { WebAuthStatus } from "../types";

const inputCls =
  "min-w-0 flex-1 rounded-lg border border-white/10 bg-black/20 px-3 py-1.5 text-xs outline-none focus:border-[var(--accent)]";

/**
 * Settings → System: optional username/password in front of the whole UI
 * (nginx basic auth, scripts/web_auth.sh). Turning it on without a password
 * sets the default "safydo", flagged until it is changed.
 */
export function WebLogin({ row }: { row: string }) {
  const [st, setSt] = useState<WebAuthStatus | null>(null);
  const [enabled, setEnabled] = useState(false);
  const [username, setUsername] = useState("safydo");
  const [password, setPassword] = useState("");
  const [show, setShow] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ text: string; error?: boolean } | null>(null);

  useEffect(() => {
    api
      .webAuth()
      .then((s) => {
        setSt(s);
        setEnabled(s.enabled);
        setUsername(s.username || "safydo");
      })
      .catch((e) => setMsg({ text: e instanceof Error ? e.message : "Unavailable", error: true }));
  }, []);

  const userOk = /^[A-Za-z0-9._-]{1,32}$/.test(username.trim());
  const changed =
    st != null && (enabled !== st.enabled || (enabled && (username.trim() !== st.username || password !== "")));

  const save = async () => {
    setBusy(true);
    setMsg(null);
    try {
      const next = await api.setWebAuth({
        enabled,
        ...(enabled ? { username: username.trim() } : {}),
        ...(enabled && password ? { password } : {}),
      });
      setSt(next);
      setPassword("");
      if (next.enabled) {
        setMsg({ text: "Saved. Reloading, so your browser asks for the new login…" });
        // The browser only prompts on a fresh request; without the reload the
        // next API call would fail with 401 and the page would look broken.
        window.setTimeout(() => window.location.reload(), 1500);
      } else {
        setMsg({ text: "Web login is off. Anyone on the network can open the UI again." });
      }
    } catch (e) {
      setMsg({ text: e instanceof Error ? e.message : "Saving failed", error: true });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-2 py-1">
      <div className={row}>
        <div className="min-w-0">
          <p className="font-medium">Ask for a login</p>
          <p className="text-xs text-mist">
            {st?.enabled
              ? `On, for user "${st.username}"`
              : "Off: anyone on your network can open this page"}
          </p>
        </div>
        <button
          type="button"
          role="switch"
          aria-checked={enabled}
          aria-label="Ask for a login"
          disabled={busy || !st}
          onClick={() => setEnabled((v) => !v)}
          className={`relative h-6 w-11 shrink-0 rounded-full transition-colors disabled:opacity-50 ${
            enabled ? "bg-[var(--accent)]" : "bg-white/15"
          }`}
        >
          <span
            className={`absolute top-0.5 h-5 w-5 rounded-full bg-white transition-all ${enabled ? "left-[1.375rem]" : "left-0.5"}`}
          />
        </button>
      </div>

      {enabled && (
        <>
          <div className="flex items-center gap-2">
            <span className="w-20 shrink-0 text-xs text-mist">Username</span>
            <input
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoComplete="username"
              autoCapitalize="off"
              spellCheck={false}
              className={inputCls}
            />
          </div>
          <div className="flex items-center gap-2">
            <span className="w-20 shrink-0 text-xs text-mist">Password</span>
            <input
              type={show ? "text" : "password"}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="new-password"
              placeholder={st?.enabled ? "unchanged" : 'empty = "safydo"'}
              className={inputCls}
            />
            <button
              type="button"
              onClick={() => setShow((v) => !v)}
              aria-label={show ? "Hide password" : "Show password"}
              className="shrink-0 rounded p-1 text-mist hover:text-white"
            >
              {show ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
            </button>
          </div>
          {!userOk && <p className="text-[11px] text-red-300">Username: 1–32 letters, digits, dot, dash or underscore</p>}
        </>
      )}

      {st?.enabled && st.default_password && (
        <p className="text-[11px] text-amber-300">
          The password is still the default "safydo". Set your own above.
        </p>
      )}

      <div className="flex items-center gap-2">
        <button
          type="button"
          disabled={busy || !changed || (enabled && !userOk)}
          onClick={() => void save()}
          className="flex items-center gap-1.5 rounded-lg bg-[var(--accent)] px-3 py-1.5 text-xs font-medium text-white disabled:opacity-40"
        >
          {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <KeyRound className="h-3.5 w-3.5" />}
          Save login
        </button>
        {msg && <span className={`text-[11px] ${msg.error ? "text-red-300" : "text-[var(--accent)]"}`}>{msg.text}</span>}
      </div>

      <p className="text-[10px] leading-relaxed text-mist">
        Covers this page, its controls and the setup page, from every device except the player itself. The
        page is plain HTTP, so the password can be read by someone watching your network: it keeps people out,
        it doesn't encrypt anything. Locked out? Over SSH: <code>sudo /opt/rpi-audio-player/scripts/web_auth.sh disable</code>
      </p>
    </div>
  );
}
