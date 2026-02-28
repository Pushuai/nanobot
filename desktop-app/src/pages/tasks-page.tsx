import { FormEvent, useState } from "react";
import { useDesktopContext } from "../context/desktop-context";

export function TasksPage() {
  const { request, isConnected } = useDesktopContext();
  const [codexText, setCodexText] = useState("");
  const [antigravityText, setAntigravityText] = useState("");
  const [prompt, setPrompt] = useState("");
  const [resumeSession, setResumeSession] = useState("");
  const [resumePrompt, setResumePrompt] = useState("continue");
  const [stopId, setStopId] = useState("");
  const [busy, setBusy] = useState(false);

  async function refresh() {
    const codex = await request<{ enabled: boolean; text: string }>("codex.list");
    setCodexText(codex.text || "");
    const ag = await request<{ enabled: boolean; text: string }>("antigravity.status");
    setAntigravityText(ag.text || "");
  }

  async function runCodex(e: FormEvent) {
    e.preventDefault();
    if (!prompt.trim()) {
      return;
    }
    setBusy(true);
    try {
      await request("codex.run", { prompt });
      setPrompt("");
      await refresh();
    } finally {
      setBusy(false);
    }
  }

  async function resumeCodex(e: FormEvent) {
    e.preventDefault();
    if (!resumeSession.trim()) {
      return;
    }
    setBusy(true);
    try {
      await request("codex.resume", {
        sessionId: resumeSession,
        prompt: resumePrompt || "continue"
      });
      await refresh();
    } finally {
      setBusy(false);
    }
  }

  async function stopCodex(e: FormEvent) {
    e.preventDefault();
    if (!stopId.trim()) {
      return;
    }
    setBusy(true);
    try {
      await request("codex.stop", { nameOrId: stopId });
      setStopId("");
      await refresh();
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="page">
      <header className="page-header">
        <h1>Tasks</h1>
        <button onClick={() => void refresh()} disabled={!isConnected || busy}>
          Refresh
        </button>
      </header>

      <div className="split">
        <div className="panel">
          <h2>Codex</h2>
          <form onSubmit={runCodex} className="stack">
            <input
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              placeholder="Run prompt"
            />
            <button disabled={busy || !isConnected}>Run</button>
          </form>
          <form onSubmit={resumeCodex} className="stack">
            <input
              value={resumeSession}
              onChange={(e) => setResumeSession(e.target.value)}
              placeholder="Session ID"
            />
            <input
              value={resumePrompt}
              onChange={(e) => setResumePrompt(e.target.value)}
              placeholder="Resume prompt"
            />
            <button disabled={busy || !isConnected}>Resume</button>
          </form>
          <form onSubmit={stopCodex} className="stack">
            <input
              value={stopId}
              onChange={(e) => setStopId(e.target.value)}
              placeholder="Run name or ID"
            />
            <button disabled={busy || !isConnected}>Stop</button>
          </form>
          <pre>{codexText || "No data"}</pre>
        </div>

        <div className="panel">
          <h2>Antigravity</h2>
          <pre>{antigravityText || "No data"}</pre>
        </div>
      </div>
    </section>
  );
}
