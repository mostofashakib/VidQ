"use client";
import React, {
  createContext,
  useContext,
  useState,
  ReactNode,
  useEffect,
} from "react";

interface AuthContextType {
  token: string | null;
  setToken: (token: string | null) => void;
  logout: () => void;
  loading: boolean;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setTokenState] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const stored =
      typeof window !== "undefined" ? sessionStorage.getItem("token") : null;
    if (stored) setTokenState(stored);
    setLoading(false);
  }, []);

  const setToken = (token: string | null) => {
    setTokenState(token);
    if (typeof window !== "undefined") {
      if (token) sessionStorage.setItem("token", token);
      else sessionStorage.removeItem("token");
    }
  };
  const logout = () => setToken(null);
  return (
    <AuthContext.Provider value={{ token, setToken, logout, loading }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
