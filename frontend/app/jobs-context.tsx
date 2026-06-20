"use client";

import {
  createContext,
  useContext,
  useEffect,
  useRef,
  useState,
  type Dispatch,
  type MutableRefObject,
  type ReactNode,
  type SetStateAction,
} from "react";
import type { CombineJobData, EnhanceJobData, TranslateJobData, TrimJobData } from "./api";

// ── Shared job types ───────────────────────────────────────────────────────────

export interface DownloadJob {
  localId: string;
  url: string;
  title?: string;
  status: "extracting" | "adding" | "queued" | "processing" | "done" | "failed" | "cancelled";
  message: string;
  jobId?: string;
  queuePosition?: number;
  abortController?: AbortController;
  phase?: string;
  recordingStartedAt?: number;
  downloadProgress?: number;
  recordingDuration?: number;
  errorDetail?: string;
}

export interface UploadJob {
  localId: string;
  filename: string;
  status: "uploading" | "queued" | "processing" | "done" | "failed" | "cancelled";
  message: string;
  progress: number;
  scaleProgress?: number;
  jobId?: string;
}

export interface CombineJobItem {
  localId: string;
  label: string;
  uploadProgress: number;
  jobId: string;
  data: CombineJobData;
}

export interface TranslateJobItem {
  localId: string;
  filename: string;
  uploadProgress: number;
  jobId: string;
  data: TranslateJobData;
}

export interface TrimJobItem {
  localId: string;
  filename: string;
  uploadProgress: number;
  jobId: string;
  data: TrimJobData;
}

export interface EnhanceJobItem {
  localId: string;
  filename: string;
  uploadProgress: number;
  jobId: string;
  data: EnhanceJobData;
}

// ── Context ────────────────────────────────────────────────────────────────────

interface JobsContextValue {
  // Download
  downloads: DownloadJob[];
  setDownloads: Dispatch<SetStateAction<DownloadJob[]>>;
  // Upload
  uploadJobs: UploadJob[];
  setUploadJobs: Dispatch<SetStateAction<UploadJob[]>>;
  uploadPollRefs: MutableRefObject<Map<string, ReturnType<typeof setInterval>>>;
  uploadAbortRefs: MutableRefObject<Map<string, () => void>>;
  // Combine
  combineJobs: CombineJobItem[];
  setCombineJobs: Dispatch<SetStateAction<CombineJobItem[]>>;
  combinePollRefs: MutableRefObject<Map<string, ReturnType<typeof setInterval>>>;
  // Translate
  translateJobs: TranslateJobItem[];
  setTranslateJobs: Dispatch<SetStateAction<TranslateJobItem[]>>;
  translatePollRefs: MutableRefObject<Map<string, ReturnType<typeof setInterval>>>;
  // Trim
  trimJobs: TrimJobItem[];
  setTrimJobs: Dispatch<SetStateAction<TrimJobItem[]>>;
  trimPollRefs: MutableRefObject<Map<string, ReturnType<typeof setInterval>>>;
  // Enhance
  enhanceJobs: EnhanceJobItem[];
  setEnhanceJobs: Dispatch<SetStateAction<EnhanceJobItem[]>>;
  enhancePollRefs: MutableRefObject<Map<string, ReturnType<typeof setInterval>>>;
}

const JobsContext = createContext<JobsContextValue | null>(null);

const TERMINAL = ["done", "failed", "cancelled"];

// Serialise only fields that survive JSON round-trip (drop AbortController etc.)
function serializeDownload(d: DownloadJob) {
  return {
    localId: d.localId,
    jobId: d.jobId,
    url: d.url,
    title: d.title,
    status: d.status,
    message: d.message,
    phase: d.phase,
  };
}

export function JobsProvider({ children }: { children: ReactNode }) {
  const [downloads, setDownloads] = useState<DownloadJob[]>([]);
  const [uploadJobs, setUploadJobs] = useState<UploadJob[]>([]);
  const [combineJobs, setCombineJobs] = useState<CombineJobItem[]>([]);
  const [translateJobs, setTranslateJobs] = useState<TranslateJobItem[]>([]);
  const [trimJobs, setTrimJobs] = useState<TrimJobItem[]>([]);
  const [enhanceJobs, setEnhanceJobs] = useState<EnhanceJobItem[]>([]);

  const uploadPollRefs = useRef<Map<string, ReturnType<typeof setInterval>>>(new Map());
  const uploadAbortRefs = useRef<Map<string, () => void>>(new Map());
  const combinePollRefs = useRef<Map<string, ReturnType<typeof setInterval>>>(new Map());
  const translatePollRefs = useRef<Map<string, ReturnType<typeof setInterval>>>(new Map());
  const trimPollRefs = useRef<Map<string, ReturnType<typeof setInterval>>>(new Map());
  const enhancePollRefs = useRef<Map<string, ReturnType<typeof setInterval>>>(new Map());

  // Track whether the initial localStorage load has completed so we don't
  // overwrite persisted data with an empty array on the very first render.
  const persistReady = useRef(false);

  // ── Restore from localStorage once on mount ──────────────────────────────
  useEffect(() => {
    try {
      const raw = localStorage.getItem("vidq_downloads");
      if (raw) {
        const parsed = JSON.parse(raw) as DownloadJob[];
        const active = parsed.filter(
          (d) => d.jobId && !TERMINAL.includes(d.status)
        );
        if (active.length) setDownloads(active);
      }
    } catch {}

    try {
      const raw = localStorage.getItem("vidq_uploads");
      if (raw) {
        const parsed = JSON.parse(raw) as UploadJob[];
        const active = parsed.filter(
          (j) => j.jobId && !TERMINAL.includes(j.status)
        );
        if (active.length) setUploadJobs(active);
      }
    } catch {}

    try {
      const raw = localStorage.getItem("vidq_combine");
      if (raw) {
        const parsed = JSON.parse(raw) as CombineJobItem[];
        const active = parsed.filter(
          (j) => j.jobId && !TERMINAL.includes(j.data.status)
        );
        if (active.length) setCombineJobs(active);
      }
    } catch {}

    try {
      const raw = localStorage.getItem("vidq_translate");
      if (raw) {
        const parsed = JSON.parse(raw) as TranslateJobItem[];
        const active = parsed.filter(
          (j) => j.jobId && !TERMINAL.includes(j.data.status)
        );
        if (active.length) setTranslateJobs(active);
      }
    } catch {}

    try {
      const raw = localStorage.getItem("vidq_trim");
      if (raw) {
        const parsed = JSON.parse(raw) as TrimJobItem[];
        const active = parsed.filter(
          (j) => j.jobId && !TERMINAL.includes(j.data.status)
        );
        if (active.length) setTrimJobs(active);
      }
    } catch {}

    try {
      const raw = localStorage.getItem("vidq_enhance");
      if (raw) {
        const parsed = JSON.parse(raw) as EnhanceJobItem[];
        const active = parsed.filter(
          (j) => j.jobId && !TERMINAL.includes(j.data.status)
        );
        if (active.length) setEnhanceJobs(active);
      }
    } catch {}

    // Allow save effects to run from here on.
    persistReady.current = true;
  }, []);

  // ── Persist active jobs to localStorage on every change ─────────────────
  useEffect(() => {
    if (!persistReady.current) return;
    const toSave = downloads
      .filter((d) => d.jobId && !TERMINAL.includes(d.status))
      .map(serializeDownload);
    localStorage.setItem("vidq_downloads", JSON.stringify(toSave));
  }, [downloads]);

  useEffect(() => {
    if (!persistReady.current) return;
    const toSave = uploadJobs.filter(
      (j) => j.jobId && !TERMINAL.includes(j.status)
    );
    localStorage.setItem("vidq_uploads", JSON.stringify(toSave));
  }, [uploadJobs]);

  useEffect(() => {
    if (!persistReady.current) return;
    const toSave = combineJobs.filter(
      (j) => j.jobId && !TERMINAL.includes(j.data.status)
    );
    localStorage.setItem("vidq_combine", JSON.stringify(toSave));
  }, [combineJobs]);

  useEffect(() => {
    if (!persistReady.current) return;
    const toSave = translateJobs.filter(
      (j) => j.jobId && !TERMINAL.includes(j.data.status)
    );
    localStorage.setItem("vidq_translate", JSON.stringify(toSave));
  }, [translateJobs]);

  useEffect(() => {
    if (!persistReady.current) return;
    const toSave = trimJobs.filter(
      (j) => j.jobId && !TERMINAL.includes(j.data.status)
    );
    localStorage.setItem("vidq_trim", JSON.stringify(toSave));
  }, [trimJobs]);

  useEffect(() => {
    if (!persistReady.current) return;
    const toSave = enhanceJobs.filter(
      (j) => j.jobId && !TERMINAL.includes(j.data.status)
    );
    localStorage.setItem("vidq_enhance", JSON.stringify(toSave));
  }, [enhanceJobs]);

  return (
    <JobsContext.Provider
      value={{
        downloads, setDownloads,
        uploadJobs, setUploadJobs,
        uploadPollRefs, uploadAbortRefs,
        combineJobs, setCombineJobs,
        combinePollRefs,
        translateJobs, setTranslateJobs,
        translatePollRefs,
        trimJobs, setTrimJobs,
        trimPollRefs,
        enhanceJobs, setEnhanceJobs,
        enhancePollRefs,
      }}
    >
      {children}
    </JobsContext.Provider>
  );
}

export function useJobs() {
  const ctx = useContext(JobsContext);
  if (!ctx) throw new Error("useJobs must be used inside JobsProvider");
  return ctx;
}
