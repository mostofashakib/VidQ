"use client";

import { useEffect, useRef, useState, useCallback } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "../auth-context";
import { useJobs, type TranslateJobItem } from "../jobs-context";
import { createLocalId, triggerFileDownload, useJobPolling } from "../job-utils";
import {
  startTranslateJob,
  getTranslateJob,
  cancelTranslateJob,
  TranslateJobData,
} from "../api";
import { Button } from "@/components/ui/button";
import { Loader2, X, Check, Download, Captions, Clock, Ban, Trash2 } from "lucide-react";
import Navbar from "@/components/Navbar";
import ResultVideoPlayer from "@/components/ResultVideoPlayer";


function phaseLabel(data: TranslateJobData): string {
  switch (data.phase) {
    case "extracting_audio":
      return "Extracting audio…";
    case "transcribing":
      return "Transcribing with Whisper…";
    case "translating":
      return data.total_chunks > 0
        ? `Translating (chunk ${data.chunk_index}/${data.total_chunks})…`
        : "Translating…";
    case "burning":
      return `Burning subtitles… ${data.overall_progress}%`;
    default:
      return "Processing…";
  }
}

function statusMessage(item: TranslateJobItem): string {
  if (item.jobId === "") {
    return item.uploadProgress < 100 ? `Uploading… ${item.uploadProgress}%` : "Waiting for server…";
  }
  const { data } = item;
  if (data.status === "queued") return "Waiting for worker…";
  if (data.status === "processing") return phaseLabel(data);
  if (data.status === "done") return "Done!";
  if (data.status === "failed") return data.error || "Failed";
  return "Processing…";
}

const PHASES = ["extracting_audio", "transcribing", "translating", "burning"];
const PHASE_LABELS: Record<string, string> = {
  extracting_audio: "Audio",
  transcribing: "Whisper",
  translating: "LLM",
  burning: "Burn",
};

export default function TranslatePage() {
  const { token, loading } = useAuth();
  const router = useRouter();

  const [file, setFile] = useState<File | null>(null);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState("");
  const fileInputRef = useRef<HTMLInputElement>(null);
  const { translateJobs: jobs, setTranslateJobs: setJobs, translatePollRefs: pollRefs } = useJobs();

  useEffect(() => {
    if (!loading && !token) router.replace("/login");
  }, [token, loading, router]);

  const { updateJob, startPolling, removeJob, cancelLocalJob } = useJobPolling({
    token,
    jobs,
    setJobs,
    pollRefs,
    getJob: getTranslateJob,
  });

  const handleFile = useCallback((incoming: FileList | null) => {
    if (!incoming || incoming.length === 0) return;
    const f = incoming[0];
    if (!f.type.startsWith("video/")) { setError("Please select a video file."); return; }
    setError("");
    setFile(f);
  }, []);

  async function handleTranslate() {
    if (!token || !file) return;
    setError("");

    const capturedFile = file;
    const localId = createLocalId();

    const initialData: TranslateJobData = {
      job_id: "",
      filename: capturedFile.name,
      status: "queued",
      phase: "uploading",
      overall_progress: 0,
      chunk_index: 0,
      total_chunks: 0,
    };

    setJobs((prev) => [
      ...prev,
      { localId, filename: capturedFile.name, uploadProgress: 0, jobId: "", data: initialData },
    ]);
    setFile(null);

    try {
      const data = await startTranslateJob(
        token,
        capturedFile,
        (pct) => updateJob(localId, { uploadProgress: pct }),
      );
      updateJob(localId, { jobId: data.job_id, data, uploadProgress: 100 });
      startPolling(localId, data.job_id);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "Translate failed";
      updateJob(localId, { data: { ...initialData, status: "failed", error: msg } });
    }
  }

  function handleCancel(localId: string) {
    cancelLocalJob(localId, cancelTranslateJob);
  }

  function handleDownload(item: TranslateJobItem) {
    if (!item.data.result_url) return;
    void triggerFileDownload(item.data.result_url, "subtitled_video.mp4")
      .catch(() => setError("Failed to download translated video."));
  }

  function handleDelete(localId: string) {
    removeJob(localId);
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
        {/* Drop zone — matches Convert style */}
        <div className="glass-panel p-6 md:p-8 rounded-4xl mb-6 shadow-2xl shadow-purple-500/5">
          <div
            onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => { e.preventDefault(); setDragging(false); handleFile(e.dataTransfer.files); }}
            onClick={() => fileInputRef.current?.click()}
            className={`border-2 border-dashed rounded-3xl flex flex-col items-center justify-center gap-3 py-16 cursor-pointer transition-all duration-300 ${
              dragging
                ? "border-indigo-400 bg-indigo-500/10"
                : "border-white/10 hover:border-indigo-500/50 hover:bg-white/5"
            }`}
          >
            <div className="w-14 h-14 rounded-full bg-indigo-500/15 flex items-center justify-center">
              <Captions className="w-6 h-6 text-indigo-400" />
            </div>
            <p className="text-white font-medium">Drop a video file here</p>
            <p className="text-gray-400 text-sm">or click to browse — English subtitles burned into the video</p>
            <input
              ref={fileInputRef}
              type="file"
              accept="video/*"
              className="hidden"
              onChange={(e) => handleFile(e.target.files)}
            />
          </div>
        </div>

        {/* Selected file + translate button */}
        {file && (
          <div className="glass-panel p-4 rounded-3xl mb-6 space-y-3">
            <div className="flex items-center justify-between gap-3 px-1">
              <div className="flex items-center gap-3 min-w-0">
                <Captions className="w-4 h-4 text-indigo-400 shrink-0" />
                <span className="text-sm text-gray-200 truncate">{file.name}</span>
                <span className="text-xs text-gray-500 shrink-0">
                  {(file.size / 1024 / 1024).toFixed(1)} MB
                </span>
              </div>
              <button
                onClick={() => setFile(null)}
                className="text-gray-500 hover:text-red-400 transition-colors shrink-0"
              >
                <X className="w-4 h-4" />
              </button>
            </div>
            <Button
              onClick={handleTranslate}
              className="w-full bg-indigo-600 hover:bg-indigo-500 text-white rounded-xl"
            >
              <Captions className="w-4 h-4 mr-2" />
              Translate &amp; Add Captions
            </Button>
          </div>
        )}

        {error && <p className="text-red-400 text-sm mb-4">{error}</p>}

        {/* Job queue list */}
        {jobs.length > 0 && (
          <div className="space-y-2.5">
            {jobs.map((item) => {
              const visualStatus = item.jobId === "" ? "uploading" : item.data.status;
              const isDone = visualStatus === "done";
              const isFailed = visualStatus === "failed";
              const isQueued = visualStatus === "queued";
              const isActive = !isDone && !isFailed && visualStatus !== "cancelled";
              const currentPhaseIdx = PHASES.indexOf(item.data.phase);

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
                    {visualStatus === "uploading" && (
                      <div className="mt-1.5 h-1 bg-white/10 rounded-full overflow-hidden">
                        <div
                          className="h-full bg-indigo-500 rounded-full transition-all duration-300"
                          style={{ width: `${item.uploadProgress}%` }}
                        />
                      </div>
                    )}
                    {isQueued && (
                      <div className="mt-1.5 h-1 bg-white/10 rounded-full overflow-hidden">
                        <div className="h-full rounded-full animate-pulse w-full bg-yellow-500/60" />
                      </div>
                    )}
                    {visualStatus === "processing" && (
                      <>
                        <div className="mt-1.5 h-1 bg-white/10 rounded-full overflow-hidden">
                          {item.data.overall_progress > 0 ? (
                            <div
                              className="h-full bg-indigo-500 rounded-full transition-all duration-500"
                              style={{ width: `${item.data.overall_progress}%` }}
                            />
                          ) : (
                            <div className="h-full rounded-full animate-pulse w-full bg-indigo-500/60" />
                          )}
                        </div>
                        <div className="flex items-center gap-1 text-xs text-gray-500 mt-1.5">
                          {PHASES.map((ph, i) => (
                            <span key={ph} className="flex items-center gap-1">
                              {i > 0 && <span className="text-gray-700">→</span>}
                              <span
                                className={
                                  i < currentPhaseIdx
                                    ? "text-green-400"
                                    : i === currentPhaseIdx
                                    ? "text-indigo-400"
                                    : "text-gray-600"
                                }
                              >
                                {PHASE_LABELS[ph]}
                              </span>
                            </span>
                          ))}
                        </div>
                      </>
                    )}
                    {isDone && item.data.result_url && (
                      <ResultVideoPlayer src={item.data.result_url} title={item.filename} />
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
