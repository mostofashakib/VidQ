"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "../auth-context";
import { login as apiLogin } from "../api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

export default function LoginPage() {
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const { setToken } = useAuth();
  const router = useRouter();
  const [loading, setLoading] = useState(false);

  async function handleLogin(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    setError("");
    try {
      const token = await apiLogin(password);
      setToken(token);
      router.push("/");
    } catch {
      setError("Incorrect password");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center p-4 text-white">
      <form
        onSubmit={handleLogin}
        className="glass-panel p-10 rounded-[2rem] shadow-2xl shadow-purple-500/10 w-full max-w-sm flex flex-col items-center border-white/10"
      >
        <div className="w-16 h-16 bg-gradient-to-tr from-indigo-500 to-purple-500 rounded-2xl flex items-center justify-center mb-6 shadow-lg shadow-indigo-500/20 transform -rotate-6 hover:rotate-0 transition-transform duration-300">
          <svg className="w-8 h-8 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z" />
          </svg>
        </div>
        <h1 className="text-3xl font-bold mb-2 tracking-tight text-center bg-clip-text text-transparent bg-gradient-to-r from-white to-gray-400">Welcome Back</h1>
        <p className="text-gray-400 text-sm mb-8 text-center">To continue, please enter your secret admin password.</p>
        
        <Input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="Password"
          className="mb-6 h-14 bg-black/20 border-white/10 focus-visible:ring-indigo-500 rounded-xl px-4 text-white placeholder:text-gray-500 w-full text-center tracking-widest text-lg transition-all focus:bg-black/40 shadow-inner"
        />
        {error && <div className="text-red-400 text-sm mb-4 bg-red-500/10 py-2 px-4 rounded-lg w-full text-center border border-red-500/20">{error}</div>}
        <Button 
          type="submit" 
          disabled={loading || !password}
          className="w-full h-14 font-semibold rounded-xl bg-gradient-to-r from-indigo-500 to-purple-600 hover:from-indigo-400 hover:to-purple-500 transition-all border-none shadow-lg shadow-indigo-500/25 text-white"
        >
          {loading ? (
            <>
              <span className="animate-spin mr-2">⏳</span> Authenticating...
            </>
          ) : (
            "Authenticate"
          )}
        </Button>
      </form>
    </div>
  );
}
