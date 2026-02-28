import { invoke } from "@tauri-apps/api/core";
import type { DesktopEnvelope, DesktopEvent, DesktopRequest, DesktopResponse } from "../types";

type PendingItem = {
  resolve: (value: unknown) => void;
  reject: (error: Error) => void;
  timer: number;
};

function isEvent(value: DesktopEnvelope): value is DesktopEvent {
  return value.type === "event";
}

function isResponse(value: DesktopEnvelope): value is DesktopResponse {
  return value.type === "response";
}

export class DesktopClient {
  private socket: WebSocket | null = null;
  private readonly pending = new Map<string, PendingItem>();
  private readonly listeners = new Set<(event: DesktopEvent) => void>();
  private idSeed = 0;
  private readonly timeoutMs = 20000;
  private token = "";

  async connect(host = "127.0.0.1", port = 18791): Promise<void> {
    if (this.socket && this.socket.readyState === WebSocket.OPEN) {
      return;
    }
    this.token = await this.resolveToken();
    const tokenQuery = this.token ? `?token=${encodeURIComponent(this.token)}` : "";
    const url = `ws://${host}:${port}/ws${tokenQuery}`;
    await new Promise<void>((resolve, reject) => {
      const ws = new WebSocket(url);
      ws.onopen = () => {
        this.socket = ws;
        resolve();
      };
      ws.onerror = () => reject(new Error(`Failed to connect: ${url}`));
      ws.onmessage = (msg) => this.handleMessage(msg.data);
      ws.onclose = () => {
        this.socket = null;
        this.rejectAllPending(new Error("Desktop socket disconnected"));
      };
    });
  }

  disconnect(): void {
    if (this.socket) {
      this.socket.close();
      this.socket = null;
    }
  }

  onEvent(listener: (event: DesktopEvent) => void): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  async request<T = unknown>(action: string, payload: Record<string, unknown> = {}): Promise<T> {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) {
      throw new Error("Desktop socket is not connected");
    }
    const requestId = String(++this.idSeed);
    const req: DesktopRequest = {
      id: requestId,
      type: "request",
      action,
      payload
    };
    return await new Promise<T>((resolve, reject) => {
      const timer = window.setTimeout(() => {
        this.pending.delete(requestId);
        reject(new Error(`Request timeout: ${action}`));
      }, this.timeoutMs);

      this.pending.set(requestId, {
        resolve: (value) => resolve(value as T),
        reject,
        timer
      });

      this.socket?.send(JSON.stringify(req));
    });
  }

  isConnected(): boolean {
    return !!this.socket && this.socket.readyState === WebSocket.OPEN;
  }

  private async resolveToken(): Promise<string> {
    try {
      const token = await invoke<string>("desktop_token");
      return String(token || "");
    } catch {
      // Browser preview mode fallback.
      return "";
    }
  }

  private handleMessage(raw: string): void {
    let obj: DesktopEnvelope;
    try {
      obj = JSON.parse(raw) as DesktopEnvelope;
    } catch {
      return;
    }

    if (isEvent(obj)) {
      this.listeners.forEach((listener) => listener(obj));
      return;
    }
    if (!isResponse(obj)) {
      return;
    }

    const responseId = String(obj.id ?? "");
    if (!responseId) {
      return;
    }
    const pending = this.pending.get(responseId);
    if (!pending) {
      return;
    }
    window.clearTimeout(pending.timer);
    this.pending.delete(responseId);
    if (obj.ok) {
      pending.resolve(obj.data);
    } else {
      pending.reject(new Error(obj.error || "Desktop request failed"));
    }
  }

  private rejectAllPending(error: Error): void {
    this.pending.forEach((item) => {
      window.clearTimeout(item.timer);
      item.reject(error);
    });
    this.pending.clear();
  }
}

export const desktopClient = new DesktopClient();
