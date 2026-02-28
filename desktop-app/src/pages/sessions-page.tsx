import { useEffect, useState } from "react";
import { useDesktopContext } from "../context/desktop-context";
import type { SessionInfo } from "../types";

type SessionDetail = {
  sessionKey: string;
  messages: Array<{ role?: string; content?: string; timestamp?: string }>;
};

export function SessionsPage() {
  const { request, isConnected } = useDesktopContext();
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [selected, setSelected] = useState<string>("");
  const [detail, setDetail] = useState<SessionDetail | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!isConnected) {
      return;
    }
    void refreshSessions();
  }, [isConnected]);

  async function refreshSessions() {
    setLoading(true);
    try {
      const data = await request<{ sessions: SessionInfo[] }>("session.list");
      setSessions(data.sessions || []);
    } finally {
      setLoading(false);
    }
  }

  async function openSession(sessionKey: string) {
    setSelected(sessionKey);
    const data = await request<SessionDetail>("session.open", { sessionKey, limit: 200 });
    setDetail(data);
  }

  return (
    <section className="page">
      <header className="page-header">
        <h1>Sessions</h1>
        <button onClick={() => void refreshSessions()} disabled={!isConnected || loading}>
          {loading ? "Refreshing..." : "Refresh"}
        </button>
      </header>
      <div className="split">
        <div className="panel">
          {sessions.map((session) => (
            <button
              className={`list-item ${selected === session.key ? "active" : ""}`}
              key={session.key}
              onClick={() => void openSession(session.key)}
            >
              <div>{session.key}</div>
              <small>{session.updated_at || "-"}</small>
            </button>
          ))}
        </div>
        <div className="panel">
          {!detail ? (
            <div className="empty">Select a session</div>
          ) : (
            detail.messages.map((msg, idx) => (
              <article key={idx} className="session-msg">
                <div className="msg-meta">
                  <span>{msg.role || "unknown"}</span>
                  <span>{msg.timestamp || ""}</span>
                </div>
                <pre>{msg.content || ""}</pre>
              </article>
            ))
          )}
        </div>
      </div>
    </section>
  );
}
