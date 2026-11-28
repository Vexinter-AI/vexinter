import { useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { isAuthed, logout } from "../api";
import { useTheme } from "../theme";
import logo from "../assets/vexinter-logo.svg";

const I = {
  panel: ["M22 12h-4l-3 9L9 3l-3 9H2"],
  executions: [
    "M12 2 2 7l10 5 10-5-10-5z",
    "M2 12l10 5 10-5",
    "M2 17l10 5 10-5",
  ],
  decisions: [
    "M22 12h-6l-2 3h-4l-2-3H2",
    "M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z",
  ],
  agents: [
    "M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2",
    "M9 3a4 4 0 1 0 0 8 4 4 0 0 0 0-8z",
    "M22 21v-2a4 4 0 0 0-3-3.87",
    "M16 3.13a4 4 0 0 1 0 7.75",
  ],
  metrics: ["M3 3v18h18", "M7 15v3", "M12 10v8", "M17 6v12"],
};
const Icon = ({ paths }) => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
    strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    {paths.map((d, i) => (
      <path key={i} d={d} />
    ))}
  </svg>
);

const ICONS = {
  sun: ["M12 17a5 5 0 1 0 0-10 5 5 0 0 0 0 10z", "M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"],
  moon: ["M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"],
  power: ["M18.36 6.64A9 9 0 1 1 5.64 6.64", "M12 2v10"],
};

export default function Layout() {
  const { t } = useTranslation();
  const { theme, setTheme } = useTheme();
  const nav = useNavigate();
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const { pathname } = useLocation();

  if (!isAuthed()) {
    nav("/login");
    return null;
  }

  const items = [
    ["/", "panel"],
    ["/executions", "executions"],
    ["/cases", "decisions"],
    ["/agents", "agents"],
    ["/metrics", "metrics"],
  ];

  return (
    <div className="app">
      {sidebarOpen && (
        <div
          className="sidebar-backdrop"
          onClick={() => setSidebarOpen(false)}
          aria-hidden="true"
        />
      )}

      <aside
        className={sidebarOpen ? "open" : ""}
        aria-label={t("nav.sidebar")}
      >
        <div
          className="logo"
          style={{ justifyContent: "space-between", alignItems: "center" }}
        >
          <img src={logo} alt="Vexinter" className="logo-img" />
          <span
            className="badge"
            style={{
              fontSize: "10px",
              padding: "2px 7px",
              background: "var(--line2)",
              color: "var(--muted)",
              fontWeight: 700,
            }}
          >
            {t("common.version")}
          </span>
        </div>

        <nav>
          {items.map(([to, k]) => (
            <NavLink
              key={to}
              to={to}
              end={to === "/"}
              className={({ isActive }) => (isActive ? "on" : "")}
              onClick={() => setSidebarOpen(false)}
            >
              <Icon paths={I[k]} />
              <span>{t(`nav.${k}`)}</span>
            </NavLink>
          ))}
        </nav>

        <div className="side-foot">
          <div className="row">
            <button
              className="tbtn icon-btn"
              title={t("common.theme")}
              onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
              aria-label={t("common.theme")}
            >
              <Icon paths={theme === "dark" ? ICONS.sun : ICONS.moon} />
            </button>
            <button
              className="tbtn icon-btn logout-btn"
              title={t("common.logout")}
              onClick={async () => {
                await logout();
                nav("/login");
              }}
              aria-label={t("common.logout")}
            >
              <Icon paths={ICONS.power} />
            </button>
          </div>
        </div>
      </aside>

      <button
        className="hamburger"
        onClick={() => setSidebarOpen(!sidebarOpen)}
        aria-expanded={sidebarOpen}
        aria-controls="sidebar"
        aria-label={t("nav.toggleSidebar")}
      >
        <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true">
          {sidebarOpen ? (
            <path
              d="M6 6l12 12M6 18l12-12"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
            />
          ) : (
            <path
              d="M4 6h16M4 12h16M4 18h16"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
            />
          )}
        </svg>
      </button>

      <main>
        <div className="page-enter" key={pathname}>
          <Outlet />
        </div>
      </main>
    </div>
  );
}
