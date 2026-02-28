import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { DesktopEvent } from "../types";
import { desktopClient } from "../lib/desktop-client";

type ConnectionState = "idle" | "connecting" | "connected" | "error";

export function useDesktop() {
  const [state, setState] = useState<ConnectionState>("idle");
  const [error, setError] = useState<string>("");
  const reconnectTimer = useRef<number | null>(null);

  const connect = useCallback(async () => {
    if (state === "connecting" || state === "connected") {
      return;
    }
    setState("connecting");
    setError("");
    try {
      await desktopClient.connect();
      setState("connected");
    } catch (e) {
      setState("error");
      setError(String(e));
      if (reconnectTimer.current) {
        window.clearTimeout(reconnectTimer.current);
      }
      reconnectTimer.current = window.setTimeout(() => {
        setState("idle");
      }, 3000);
    }
  }, [state]);

  const disconnect = useCallback(() => {
    if (reconnectTimer.current) {
      window.clearTimeout(reconnectTimer.current);
      reconnectTimer.current = null;
    }
    desktopClient.disconnect();
    setState("idle");
  }, []);

  const request = useCallback(
    async <T,>(action: string, payload: Record<string, unknown> = {}) => {
      return await desktopClient.request<T>(action, payload);
    },
    []
  );

  const onEvent = useCallback((listener: (event: DesktopEvent) => void) => {
    return desktopClient.onEvent(listener);
  }, []);

  useEffect(() => {
    void connect();
    return () => {
      disconnect();
    };
  }, [connect, disconnect]);

  return useMemo(
    () => ({
      state,
      error,
      isConnected: state === "connected",
      connect,
      disconnect,
      request,
      onEvent
    }),
    [state, error, connect, disconnect, request, onEvent]
  );
}
