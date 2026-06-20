"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "../auth-context";
import { useJobs, type EnhanceJobItem } from "../jobs-context";
import { createLocalId, triggerFileDownload, useJobPolling } from "../job-utils";
import { startEnhanceJob, getEnhanceJob, cancelEnhanceJob, type EnhanceJobData } from "../api";
import { Button } from "@/components/ui/button";
import { Loader2, X, Check, Download, Sparkles, Clock, Ban, Trash2 } from "lucide-react";
import Navbar from "@/components/Navbar";

function statusMessage(item: EnhanceJobItem): string {
  if (item.jobId === "") {
    return item.uploadProgress < 100
      ? `Uploading… ${item.uploadProgress}%`
      : "Waiting for server…";
  }
  const { data } = item;
  if (data.status === "queued") return "Queued…";
  if (data.status === "processing") {
    const phase = data.phase;
    if (phase === "splitting") return "Splitting video…";
    if (phase === "assembling") return "Assembling final video…";
    if (phase.startsWith("enhancing")) {
      const chunk = phase.split(" ")[1] ?? "1/1";
      const [n, total] = chunk.split("/");
      return `Enhancing chunk ${n} of ${total} — ${data.progress}%`;
    }
    return `Processing… ${data.progress}%`;
  }
  if (data.status === "done") return "Done!";
  if (data.status === "failed") return data.error || "Enhancement failed";
  return "Processing…";
}

export default function EnhancePage() {
  const { token, loading } = useAuth();
  const router = useRouter();

  const [file, setFile] = useState<File | null>(null);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState("");
  const fileInputRef = useRef<HTMLInputElement>(null);

  const {
    enhanceJobs: jobs,
    setEnhanceJobs: setJobs,
    enhancePollRefs: pollRefs,
  } = useJobs();

  useEffect(() => {
    if (!loading && !token) router.replace("/login");
  }, [token, loading, router]);

  const { updateJob, startPolling, removeJob, cancelLocalJob } = useJobPolling({
    token,
    jobs,
    setJobs,
    pollRefs,
    getJob: getEnhanceJob,
    intervalMs: 3000,
  });

  function handleFile(f: File) {
    if (!f.type.startsWith("video/")) {
      setError("Please select a video file.");
      return;
    }
    setError("");
    setFile(f);
  }

  function handleDrop(e: React.DragEvent) {
    e.preventDefault();
    setDragging(false);
    const f = e.dataTransfer.files[0];
    if (f) handleFile(f);
  }

  async function handleEnhance() {
    if (!token || !file) return;
    setError("");
    const capturedFile = file;
    const localId = createLocalId();
    const initialData: EnhanceJobData = {
      job_id: "",
      status: "queued",
      phase: "queued",
      progress: 0,
    };

    setJobs((prev) => [
      ...prev,
      { localId, filename: capturedFile.name, uploadProgress: 0, jobId: "", data: initialData },
    ]);
    setFile(null);

    try {
      const data = await startEnhanceJob(
        token,
        capturedFile,
        (pct) => updateJob(localId, { uploadProgress: pct }),
      );
      updateJob(localId, { jobId: data.job_id, data, uploadProgress: 100 });
      startPolling(localId, data.job_id);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "Enhancement failed";
      updateJob(localId, { data: { ...initialData, status: "failed", error: msg } });
    }
  }

  function handleCancel(localId: string) {
    cancelLocalJob(localId, cancelEnhanceJob);
  }

  function handleDelete(localId: string) {
    removeJob(localId);
  }

  function handleDownload(item: EnhanceJobItem) {
    if (!item.data.result_url) return;
    triggerFileDownload(item.data.result_url, `enhanced_${item.filename}`);
  }

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center text-white">
        <Loader2 className="animate-spin w-8 h-8 text-indigo-400" />
      </div>
    );
  }

  return (
    <div className="min-h-screen text-white pb-20">
      <Navbar />
      <div className="max-w-3xl mx-auto px-4 sm:px-6">

        {/* Upload panel */}
        <div className="glass-panel p-6 md:p-8 rounded-4xl mb-8 shadow-2xl shadow-purple-500/5">
          <input
            ref={fileInputRef}
            type="file"
            accept="video/*"
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) handleFile(f);
              e.target.value = "";
            }}
          />
          <div
            onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
            onDragLeave={() => setDragging(false)}
            onDrop={handleDrop}
            onClick={() => !file && fileInputRef.current?.click()}
            className={`border-2 border-dashed rounded-3xl flex flex-col items-center justify-center gap-3 py-16 transition-all duration-300 ${
              file
                ? "border-indigo-500/50 bg-indigo-500/5 cursor-default"
                : dragging
                ? "border-indigo-400 bg-indigo-500/10 cursor-pointer"
                : "border-white/10 hover:border-indigo-500/50 hover:bg-white/5 cursor-pointer"
            }`}
          >
            <div className="w-14 h-14 rounded-full bg-indigo-500/15 flex items-center justify-center">
              <Sparkles className="w-6 h-6 text-indigo-400" />
            </div>
            {file ? (
              <>
                <p className="text-white font-medium">{file.name}</p>
                <p className="text-gray-400 text-sm">Ready to enhance</p>
              </>
            ) : (
              <>
                <p className="text-white font-medium">Drop a video here or click to browse</p>
                <p className="text-gray-400 text-sm">
                  AI restores quality — removes noise, grain, and compression artifacts
                </p>
              </>
            )}
          </div>
          {error && <p className="text-red-400 text-sm mt-3">{error}</p>}
          <div className="flex gap-3 mt-4">
            {file && (
              <Button
                variant="outline"
                onClick={() => setFile(null)}
                className="border-white/10 bg-white/5 hover:bg-white/10 text-white rounded-xl"
              >
                Clear
              </Button>
            )}
            <Button
              onClick={handleEnhance}
              disabled={!file}
              className="flex-1 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white rounded-xl"
            >
              <Sparkles className="w-4 h-4 mr-2" />
              Enhance Video
            </Button>
          </div>
        </div>

        {/* Job library */}
        {jobs.length > 0 && (
          <div className="space-y-2.5">
            {jobs.map((item) => {
              const visualStatus = item.jobId === "" ? "uploading" : item.data.status;
              const isDone = visualStatus === "done";
              const isFailed = visualStatus === "failed";
              const isQueued = visualStatus === "queued";
              const isActive = !isDone && !isFailed && visualStatus !== "cancelled";

              return (
                <div
                  key={item.localId}
                  className={`glass-panel px-5 py-4 rounded-2xl border flex items-center gap-4 transition-all ${
                    isDone
                      ? "border-green-500/25"
                      : isFailed
                      ? "border-red-500/25"
                      : "border-indigo-500/20"
                  }`}
                >
                  <div className="shrink-0">
                    {isDone ? (
                      <div className="w-8 h-8 rounded-full bg-green-500/15 flex items-center justify-center">
                        <Check className="w-4 h-4 text-green-400" />
                      </div>
                    ) : isFailed ? (
                      <div className="w-8 h-8 rounded-full bg-red-500/15 flex items-center justify-center">
                        <X className="w-4 h-4 text-red-400" />
                      </div>
                    ) : isQueued ? (
                      <div className="w-8 h-8 rounded-full bg-yellow-500/15 flex items-center justify-center">
                        <Clock className="w-4 h-4 text-yellow-400" />
                      </div>
                    ) : (
                      <div className="w-8 h-8 rounded-full bg-indigo-500/15 flex items-center justify-center">
                        <Loader2 className="w-4 h-4 text-indigo-400 animate-spin" />
                      </div>
                    )}
                  </div>

                  <div className="flex-1 min-w-0">
                    <p className="text-sm font-medium text-white truncate">{item.filename}</p>
                    <p
                      className={`text-xs mt-0.5 ${
                        isDone
                          ? "text-green-400"
                          : isFailed
                          ? "text-red-400"
                          : isQueued
                          ? "text-yellow-400"
                          : "text-indigo-400"
                      }`}
                    >
                      {statusMessage(item)}
                    </p>
                    {isActive && (
                      <div className="mt-2 h-1 bg-white/5 rounded-full overflow-hidden">
                        {item.jobId === "" ? (
                          <div
                            className="h-full bg-indigo-500 rounded-full transition-all duration-300"
                            style={{ width: `${item.uploadProgress}%` }}
                          />
                        ) : item.data.progress > 0 ? (
                          <div
                            className="h-full bg-indigo-500 rounded-full transition-all duration-500"
                            style={{ width: `${item.data.progress}%` }}
                          />
                        ) : isQueued ? (
                          <div className="h-full rounded-full animate-pulse w-full bg-yellow-500/60" />
                        ) : (
                          <div className="h-full w-full bg-linear-to-r from-indigo-500/0 via-indigo-500/60 to-indigo-500/0 animate-pulse rounded-full" />
                        )}
                      </div>
                    )}
                  </div>

                  <div className="flex items-center gap-2 shrink-0">
                    {isDone && (
                      <button
                        title="Download"
                        onClick={() => handleDownload(item)}
                        className="h-8 w-8 flex items-center justify-center rounded-full bg-indigo-500/10 text-indigo-400 hover:bg-indigo-500 hover:text-white border border-indigo-500/20 transition-all"
                      >
                        <Download className="w-4 h-4" />
                      </button>
                    )}
                    {isDone && (
                      <button
                        title="Delete"
                        onClick={() => handleDelete(item.localId)}
                        className="h-8 w-8 flex items-center justify-center rounded-full bg-red-500/10 text-red-400 hover:bg-red-500 hover:text-white border border-red-500/20 transition-all"
                      >
                        <Trash2 className="w-4 h-4" />
                      </button>
                    )}
                    {isActive && (
                      <button
                        title="Cancel"
                        onClick={() => handleCancel(item.localId)}
                        className="text-gray-500 hover:text-red-400 transition-colors"
                      >
                        <Ban className="w-4 h-4" />
                      </button>
                    )}
                    {isFailed && (
                      <button
                        onClick={() => handleDelete(item.localId)}
                        className="text-gray-500 hover:text-white transition-colors"
                      >
                        <X className="w-4 h-4" />
                      </button>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
