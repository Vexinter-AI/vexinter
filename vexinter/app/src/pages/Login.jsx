import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { login } from "../api";
import logo from "../assets/vexinter-logo.svg";

export default function Login() {
  const { t } = useTranslation();
  const [tok, setTok] = useState("");
  const [err, setErr] = useState(false);
  const [busy, setBusy] = useState(false);
  const nav = useNavigate();

  async function submit(e) {
    e.preventDefault();
    setBusy(true);
    setErr(false);
    try {
      await login(tok);
      nav("/");
    } catch {
      setErr(true);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-wrap">
      <form className="glass login-card" onSubmit={submit}>
        <div className="logo" style={{ justifyContent: 'center', marginBottom: 8 }}>
          <img src={logo} alt="Vexinter" className="logo-img" style={{ height: 38 }} />
        </div>
        <p className="muted" style={{ marginBottom: 20, textAlign: 'center' }}>
          {t("common.consoleSub")}
        </p>
        <p className="muted" style={{ marginBottom: 24, fontSize: '11px', textAlign: 'center' }}>
          {t("common.version")} · {t("common.status")}
        </p>
        <label htmlFor="token">{t("common.tokenLabel")}</label>
        <input
          id="token"
          type="password"
          autoFocus
          placeholder={t("common.tokenPh")}
          value={tok}
          onChange={(e) => setTok(e.target.value)}
        />
        <button
          className="btn btn-primary"
          style={{ width: "100%", marginTop: 18 }}
          disabled={busy || !tok}
        >
          {t("common.enter")}
        </button>
        {err && <div className="err" style={{ marginTop: 12 }}>{t("common.badToken")}</div>}
      </form>
    </div>
  );
}