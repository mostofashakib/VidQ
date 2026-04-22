"use client";
import React, {
  createContext,
  useContext,
  useState,
  ReactNode,
  useEffect,
} from "react";
import { getAuthStatus } from "./api";

interface AuthContextType {
  token: string | null;
  setToken: (token: string | null) => void;
  logout: () => void;
  loading: boolean;
  authEnabled: boolean;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setTokenState] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [authEnabled, setAuthEnabled] = useState(true);

  useEffect(() => {
    async function init() {
      try {
        const { auth_enabled } = await getAuthStatus();
        setAuthEnabled(auth_enabled);

        if (!auth_enabled) {
          setTokenState("temp-bypass-token");
        } else {
          const stored =
            typeof window !== "undefined"
              ? sessionStorage.getItem("token")
              : null;
          if (stored) setTokenState(stored);
        }
      } catch {
        // Backend unreachable — fall back to stored token
        const stored =
          typeof window !== "undefined"
            ? sessionStorage.getItem("token")
            : null;
        if (stored) setTokenState(stored);
      }
      setLoading(false);
    }
    init();
  }, []);

  const setToken = (t: string | null) => {
    setTokenState(t);
    if (typeof window !== "undefined") {
      if (t) sessionStorage.setItem("token", t);
      else sessionStorage.removeItem("token");
    }
  };

  const logout = () => setToken(null);

  return (
    <AuthContext.Provider value={{ token, setToken, logout, loading, authEnabled }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
