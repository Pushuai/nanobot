import { createContext, useContext } from "react";
import type { ReactNode } from "react";
import { useDesktop } from "../hooks/use-desktop";

type DesktopContextValue = ReturnType<typeof useDesktop>;

const DesktopContext = createContext<DesktopContextValue | null>(null);

export function DesktopProvider({ children }: { children: ReactNode }) {
  const desktop = useDesktop();
  return <DesktopContext.Provider value={desktop}>{children}</DesktopContext.Provider>;
}

export function useDesktopContext(): DesktopContextValue {
  const ctx = useContext(DesktopContext);
  if (!ctx) {
    throw new Error("useDesktopContext must be used within DesktopProvider");
  }
  return ctx;
}
