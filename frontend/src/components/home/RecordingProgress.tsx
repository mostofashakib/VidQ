"use client";

import { useEffect, useState } from "react";

export function RecordingTimer({ startedAt }: { startedAt: number }) {
  const [elapsed, setElapsed] = useState(() => Math.floor((Date.now() - startedAt) / 1000));

  useEffect(() => {
    const id = setInterval(() => setElapsed(Math.floor((Date.now() - startedAt) / 1000)), 1000);
    return () => clearInterval(id);
  }, [startedAt]);

  const m = Math.floor(elapsed / 60);
  const s = elapsed % 60;
  return <span>{m}:{s.toString().padStart(2, "0")}</span>;
}

export function RecordingProgress({
  startedAt,
  durationSeconds,
}: {
  startedAt: number;
  durationSeconds: number;
}) {
  const [pct, setPct] = useState(() =>
    Math.min(99, Math.floor(((Date.now() - startedAt) / (durationSeconds * 1000)) * 100))
  );

  useEffect(() => {
    const id = setInterval(() => {
      setPct(Math.min(99, Math.floor(((Date.now() - startedAt) / (durationSeconds * 1000)) * 100)));
    }, 1000);
    return () => clearInterval(id);
  }, [startedAt, durationSeconds]);

  return (
    <div
      className="h-full bg-red-500 rounded-full transition-all duration-1000"
      style={{ width: `${pct}%` }}
    />
  );
}
