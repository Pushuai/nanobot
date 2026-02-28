import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { App } from "./app";
import { DesktopProvider } from "./context/desktop-context";
import "./styles.css";

const queryClient = new QueryClient();

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <DesktopProvider>
        <BrowserRouter>
          <App />
        </BrowserRouter>
      </DesktopProvider>
    </QueryClientProvider>
  </React.StrictMode>
);
