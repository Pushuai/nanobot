import { Navigate, Route, Routes } from "react-router-dom";
import { AppShell } from "./components/app-shell";
import { useDesktopContext } from "./context/desktop-context";
import { ChatPage } from "./pages/chat-page";
import { IntegrationsPage } from "./pages/integrations-page";
import { LogsPage } from "./pages/logs-page";
import { SessionsPage } from "./pages/sessions-page";
import { SettingsPage } from "./pages/settings-page";
import { TasksPage } from "./pages/tasks-page";

export function App() {
  const { state, error } = useDesktopContext();

  return (
    <AppShell status={state} error={error}>
      <Routes>
        <Route path="/" element={<ChatPage />} />
        <Route path="/sessions" element={<SessionsPage />} />
        <Route path="/integrations" element={<IntegrationsPage />} />
        <Route path="/tasks" element={<TasksPage />} />
        <Route path="/settings" element={<SettingsPage />} />
        <Route path="/logs" element={<LogsPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </AppShell>
  );
}
