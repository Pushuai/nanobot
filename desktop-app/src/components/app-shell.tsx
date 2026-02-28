import { NavLink } from "react-router-dom";

const NAV_ITEMS = [
  { to: "/", label: "Chat" },
  { to: "/sessions", label: "Sessions" },
  { to: "/integrations", label: "Integrations" },
  { to: "/tasks", label: "Tasks" },
  { to: "/settings", label: "Settings" },
  { to: "/logs", label: "Logs" }
];

export function AppShell({
  children,
  status,
  error
}: {
  children: React.ReactNode;
  status: string;
  error: string;
}) {
  return (
    <div className="layout-root">
      <aside className="side-nav">
        <div className="brand">Nanobot Desktop</div>
        <nav>
          {NAV_ITEMS.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === "/"}
              className={({ isActive }) => (isActive ? "nav-item active" : "nav-item")}
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="status-card">
          <div>Connection: {status}</div>
          {error ? <div className="status-error">{error}</div> : null}
        </div>
      </aside>
      <main className="main-pane">{children}</main>
    </div>
  );
}
