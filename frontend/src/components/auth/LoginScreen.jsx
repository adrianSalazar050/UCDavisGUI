import { useState } from "react";
import Button from "../ui/Button.jsx";
import { login } from "../../api/printer.js";

// Shown only when the server is bound beyond loopback and demands a session.
// A localhost/desktop server never renders this (see App's auth probe).
export default function LoginScreen({ onSuccess }) {
  const [password, setPassword] = useState("");
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      await login(password);
      onSuccess();
    } catch (e) {
      setErr(String(e.message ?? e));
      setPassword("");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login">
      <form className="login__card add-form" onSubmit={submit}>
        <h1 className="login__title">Bambu Monitor</h1>
        <p className="login__hint">
          This dashboard is shared on the lab network. Enter the password to
          continue.
        </p>
        <input
          type="password"
          autoFocus
          value={password}
          placeholder="Password"
          onChange={(e) => setPassword(e.target.value)}
        />
        {err && <div className="add-form__error">{err}</div>}
        <Button type="submit" variant="primary" busy={busy}
                disabled={!password || busy}>
          Sign in
        </Button>
      </form>
    </div>
  );
}
