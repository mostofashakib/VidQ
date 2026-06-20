"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useAuth } from "../../app/auth-context";
import { Button } from "@/components/ui/button";

const NAV_LINKS = [
  { href: "/", label: "Download" },
  { href: "/upload", label: "Convert" },
  { href: "/combine", label: "Combine" },
  { href: "/translate", label: "Translate" },
  { href: "/trim", label: "Trim" },
  { href: "/enhance", label: "Enhance" },
];

export default function Navbar() {
  const { authEnabled, logout } = useAuth();
  const pathname = usePathname();

  return (
    <div className="flex justify-between items-center px-8 py-5 glass-panel sticky top-0 z-50 rounded-b-2xl mx-4 mb-10 shadow-xl shadow-indigo-500/10">
      <div className="flex items-center gap-6">
        <Link href="/">
          <span className="text-2xl font-bold bg-clip-text text-transparent bg-linear-to-r from-indigo-400 to-purple-400 cursor-pointer">
            VidQ
          </span>
        </Link>
        <nav className="hidden sm:flex items-center gap-1">
          {NAV_LINKS.map(({ href, label }) => {
            const active = pathname === href;
            return (
              <Link key={href} href={href}>
                <span
                  className={`px-3 py-1 text-sm rounded-lg transition-all cursor-pointer ${
                    active
                      ? "text-indigo-400 border-b-2 border-indigo-400 rounded-none"
                      : "text-gray-400 hover:text-gray-100"
                  }`}
                >
                  {label}
                </span>
              </Link>
            );
          })}
        </nav>
      </div>
      {authEnabled && (
        <Button
          variant="outline"
          onClick={logout}
          className="border-white/10 bg-transparent hover:bg-white/10 hover:text-white transition-all rounded-xl text-gray-200"
        >
          Logout
        </Button>
      )}
    </div>
  );
}
