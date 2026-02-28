import { useState } from "react";
import { useDesktopContext } from "../context/desktop-context";

export function LogsPage() {
  const { request, isConnected } = useDesktopContext();
  const [health, setHealth] = useState("");
  const [paths, setPaths] = useState("");
  const [notice, setNotice] = useState("");
  const [exporting, setExporting] = useState(false);

  async function refreshHealth() {
    const data = await request<Record<string, unknown>>("app.health");
    setHealth(JSON.stringify(data, null, 2));
  }

  async function loadPaths() {
    const data = await request<{ root: string; files: string[] }>("diagnostics.paths");
    setPaths(JSON.stringify(data, null, 2));
  }

  async function exportDiagnostics() {
    setExporting(true);
    setNotice("");
    try {
      const data = await request<{ archive: string; count: number }>("diagnostics.export");
      setNotice(`Diagnostics exported: ${data.archive} (${data.count} files)`);
    } catch (e) {
      setNotice(`Export failed: ${String(e)}`);
    } finally {
      setExporting(false);
    }
  }

  return (
    <section className="page">
      <header className="page-header">
        <h1>Logs & Diagnostics</h1>
        <div className="row">
          <button onClick={() => void refreshHealth()} disabled={!isConnected}>
            Refresh Health
          </button>
          <button onClick={() => void loadPaths()} disabled={!isConnected}>
            Show Diagnostic Paths
          </button>
          <button onClick={() => void exportDiagnostics()} disabled={!isConnected || exporting}>
            {exporting ? "Exporting..." : "Export Diagnostics"}
          </button>
        </div>
      </header>
      {notice ? <div className="notice">{notice}</div> : null}
      <pre>{health || "No health data loaded."}</pre>
      <pre>{paths || "No diagnostics paths loaded."}</pre>
    </section>
  );
}
