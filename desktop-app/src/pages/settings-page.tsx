import { useEffect, useState } from "react";
import { useDesktopContext } from "../context/desktop-context";

export function SettingsPage() {
  const { request, isConnected } = useDesktopContext();
  const [configText, setConfigText] = useState("{}");
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState("");

  useEffect(() => {
    if (!isConnected) {
      return;
    }
    void load();
  }, [isConnected]);

  async function load() {
    const config = await request<Record<string, unknown>>("config.get");
    setConfigText(JSON.stringify(config, null, 2));
  }

  async function savePatch() {
    setSaving(true);
    setNotice("");
    try {
      const parsed = JSON.parse(configText) as Record<string, unknown>;
      await request("config.patch", { patch: parsed });
      setNotice("Config saved. Restart desktop-gateway to apply all changes.");
    } catch (e) {
      setNotice(`Save failed: ${String(e)}`);
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="page">
      <header className="page-header">
        <h1>Settings</h1>
        <div className="row">
          <button onClick={() => void load()} disabled={!isConnected || saving}>
            Reload
          </button>
          <button onClick={() => void savePatch()} disabled={!isConnected || saving}>
            {saving ? "Saving..." : "Save"}
          </button>
        </div>
      </header>
      {notice ? <div className="notice">{notice}</div> : null}
      <textarea
        className="editor"
        value={configText}
        onChange={(e) => setConfigText(e.target.value)}
      />
    </section>
  );
}
