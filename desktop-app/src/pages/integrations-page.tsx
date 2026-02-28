import { useEffect, useState } from "react";
import { useDesktopContext } from "../context/desktop-context";

type ChannelStatus = Record<string, { enabled: boolean; running: boolean }>;

export function IntegrationsPage() {
  const { request, isConnected } = useDesktopContext();
  const [channels, setChannels] = useState<ChannelStatus>({});
  const [saving, setSaving] = useState<string>("");

  useEffect(() => {
    if (!isConnected) {
      return;
    }
    void loadChannels();
  }, [isConnected]);

  async function loadChannels() {
    const data = await request<{ channels: ChannelStatus }>("channel.status");
    setChannels(data.channels || {});
  }

  async function toggle(channel: string, enabled: boolean) {
    setSaving(channel);
    try {
      await request("channel.toggle", { channel, enabled });
      await loadChannels();
    } finally {
      setSaving("");
    }
  }

  return (
    <section className="page">
      <header className="page-header">
        <h1>Integrations</h1>
        <button onClick={() => void loadChannels()} disabled={!isConnected}>
          Refresh
        </button>
      </header>
      <div className="panel">
        {Object.entries(channels).map(([name, state]) => (
          <div className="row" key={name}>
            <div>
              <strong>{name}</strong>
              <div className="dim">running: {String(state.running)}</div>
            </div>
            <button
              disabled={name === "desktop" || saving === name}
              onClick={() => void toggle(name, !state.enabled)}
            >
              {saving === name ? "Saving..." : state.enabled ? "Disable" : "Enable"}
            </button>
          </div>
        ))}
      </div>
    </section>
  );
}
