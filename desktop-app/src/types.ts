export type DesktopRequest = {
  id: string;
  type: "request";
  action: string;
  payload: Record<string, unknown>;
};

export type DesktopResponse<T = unknown> = {
  id: string | null;
  type: "response";
  ok: boolean;
  data?: T;
  error?: string;
};

export type DesktopEvent = {
  type: "event";
  event: string;
  data?: unknown;
};

export type DesktopEnvelope = DesktopResponse | DesktopEvent;

export type SessionInfo = {
  key: string;
  created_at?: string;
  updated_at?: string;
  path?: string;
};

export type ChatMessage = {
  role?: string;
  content: string;
  timestamp?: string;
};
