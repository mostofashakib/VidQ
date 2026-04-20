import type { NextConfig } from "next";

if (typeof global !== 'undefined' && typeof global.localStorage !== 'undefined' && typeof global.localStorage.getItem !== 'function') {
  Object.defineProperty(global, 'localStorage', {
    value: { getItem: () => null, setItem: () => {}, removeItem: () => {}, clear: () => {} },
    writable: true, configurable: true
  });
}

const nextConfig: NextConfig = {
  devIndicators: false,
};

export default nextConfig;
