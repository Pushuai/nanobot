import { FormEvent, useEffect, useMemo, useState } from "react";
import { useDesktopContext } from "../context/desktop-context";

type UiMessage = {
  role: "user" | "assistant" | "system";
  content: string;
  ts: string;
};

export function ChatPage() {
  const { request, onEvent, isConnected } = useDesktopContext();
  const [messages, setMessages] = useState<UiMessage[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);

  useEffect(() => {
    const unsub = onEvent((event) => {
      if (event.event !== "chat.delta" && event.event !== "task.updated" && event.event !== "system.notice") {
        return;
      }
      const data = (event.data || {}) as Record<string, unknown>;
      const content = String(data.content || data.message || "");
      if (!content) {
        return;
      }
      setMessages((prev) => [
        ...prev,
        {
          role: "assistant",
          content,
          ts: new Date().toISOString()
        }
      ]);
    });
    return () => {
      unsub();
    };
  }, [onEvent]);

  const disabled = useMemo(() => !isConnected || !input.trim() || sending, [isConnected, input, sending]);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    const content = input.trim();
    if (!content) {
      return;
    }
    setSending(true);
    setInput("");
    setMessages((prev) => [
      ...prev,
      { role: "user", content, ts: new Date().toISOString() }
    ]);
    try {
      await request("chat.send", { content });
    } catch (err) {
      setMessages((prev) => [
        ...prev,
        {
          role: "system",
          content: `Send failed: ${String(err)}`,
          ts: new Date().toISOString()
        }
      ]);
    } finally {
      setSending(false);
    }
  }

  return (
    <section className="page">
      <header className="page-header">
        <h1>Chat</h1>
      </header>
      <div className="chat-stream">
        {messages.map((msg, idx) => (
          <article key={`${msg.ts}-${idx}`} className={`chat-msg ${msg.role}`}>
            <div className="msg-meta">
              <span>{msg.role}</span>
              <span>{new Date(msg.ts).toLocaleTimeString()}</span>
            </div>
            <pre>{msg.content}</pre>
          </article>
        ))}
      </div>
      <form className="chat-input" onSubmit={onSubmit}>
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Type your message..."
          rows={4}
        />
        <button type="submit" disabled={disabled}>
          {sending ? "Sending..." : "Send"}
        </button>
      </form>
    </section>
  );
}
