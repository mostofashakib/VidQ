"use client";

import { useCallback, useEffect, type Dispatch, type MutableRefObject, type SetStateAction } from "react";

const TERMINAL_STATUSES = ["done", "failed", "cancelled"];

export function createLocalId(): string {
  return `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export function isTerminalStatus(status: string): boolean {
  return TERMINAL_STATUSES.includes(status);
}

export function triggerFileDownload(url: string, filename: string): void {
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
}

export function downloadBlob(blob: Blob, filename: string): void {
  const blobUrl = window.URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = blobUrl;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  window.URL.revokeObjectURL(blobUrl);
}

export function videoDownloadName(
  video: { id: number; title?: string; url: string },
  fallbackPrefix: string,
  titleSuffix = "",
): string {
  const ext = video.url.split(".").pop()?.split("?")[0] || "mp4";
  if (!video.title) return `${fallbackPrefix}-${video.id}.${ext}`;
  const safeTitle = video.title.replace(/[^a-z0-9]/gi, "_").toLowerCase();
  return `${safeTitle}${titleSuffix}.${ext}`;
}

interface PollableJob<TData> {
  localId: string;
  jobId: string;
  data: TData;
}

interface UseJobPollingOptions<TData extends { status: string }, TJob extends PollableJob<TData>> {
  token: string | null;
  jobs: TJob[];
  setJobs: Dispatch<SetStateAction<TJob[]>>;
  pollRefs: MutableRefObject<Map<string, ReturnType<typeof setInterval>>>;
  getJob: (token: string, jobId: string) => Promise<TData>;
  intervalMs?: number;
}

export function useJobPolling<TData extends { status: string }, TJob extends PollableJob<TData>>({
  token,
  jobs,
  setJobs,
  pollRefs,
  getJob,
  intervalMs = 2000,
}: UseJobPollingOptions<TData, TJob>) {
  const updateJob = useCallback(
    (localId: string, patch: Partial<TJob>) => {
      setJobs((prev) => prev.map((job) => (job.localId === localId ? { ...job, ...patch } : job)));
    },
    [setJobs],
  );

  const removeJob = useCallback(
    (localId: string) => {
      setJobs((prev) => prev.filter((job) => job.localId !== localId));
    },
    [setJobs],
  );

  const stopPolling = useCallback(
    (localId: string) => {
      const interval = pollRefs.current.get(localId);
      if (!interval) return;
      clearInterval(interval);
      pollRefs.current.delete(localId);
    },
    [pollRefs],
  );

  const startPolling = useCallback(
    (localId: string, jobId: string) => {
      if (pollRefs.current.has(localId)) return;
      const intervalId = setInterval(async () => {
        if (!token) return;
        try {
          const data = await getJob(token, jobId);
          updateJob(localId, { data } as Partial<TJob>);
          if (isTerminalStatus(data.status)) {
            stopPolling(localId);
            if (data.status === "cancelled") removeJob(localId);
          }
        } catch {
          // ignore transient poll errors
        }
      }, intervalMs);
      pollRefs.current.set(localId, intervalId);
    },
    [getJob, intervalMs, pollRefs, removeJob, stopPolling, token, updateJob],
  );

  const cancelLocalJob = useCallback(
    (localId: string, cancelJob: (token: string, jobId: string) => Promise<void>) => {
      stopPolling(localId);
      const job = jobs.find((item) => item.localId === localId);
      if (job?.jobId && token) cancelJob(token, job.jobId).catch(() => {});
      removeJob(localId);
    },
    [jobs, removeJob, stopPolling, token],
  );

  useEffect(() => {
    if (!token) return;
    jobs.forEach((job) => {
      if (job.jobId && !pollRefs.current.has(job.localId) && !isTerminalStatus(job.data.status)) {
        startPolling(job.localId, job.jobId);
      }
    });
  }, [jobs, pollRefs, startPolling, token]);

  return { updateJob, startPolling, stopPolling, removeJob, cancelLocalJob };
}
