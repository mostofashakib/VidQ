"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "../auth-context";
import { useJobs, type CombineJobItem } from "../jobs-context";
import { createLocalId, triggerFileDownload, useJobPolling } from "../job-utils";
import {
  startCombineJob,
  getCombineJob,
  cancelCombineJob,
  CombineJobData,
} from "../api";
import { Button } from "@/components/ui/button";
import { Loader2, X, Check, Download, Film, Clock, Ban, ChevronUp, ChevronDown, Trash2 } from "lucide-react";
import Navbar from "@/components/Navbar";
import ResultVideoPlayer from "@/components/ResultVideoPlayer";


function phaseLabel(phase: string, data: CombineJobData): string {
  switch (phase) {
    case "normalizing":
      return `Normalizing clip ${data.clip_index}/${data.total_clips} to 720p…`;
    case "concatenating":
      return `Merging with crossfade… ${data.overall_progress}%`;
    default:
      return "Processing…";
  }
}

function statusMessage(item: CombineJobItem): string {
  if (item.jobId === "") {
    return item.uploadProgress < 100 ? `Uploading… ${item.uploadProgress}%` : "Waiting for server…";
  }
  const { data } = item;
  if (data.status === "queued") return "Waiting for worker…";
  if (data.status === "processing") return phaseLabel(data.phase, data);
  if (data.status === "done") return "Done!";
  if (data.status === "failed") return data.error || "Failed";
  return "Processing…";
}

export default function CombinePage() {
  const { token, loading } = useAuth();
  const router = useRouter();

  const [files, setFiles] = useState<File[]>([]);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState("");
  const { combineJobs: jobs, setCombineJobs: setJobs, combinePollRefs: pollRefs } = useJobs();

  useEffect(() => {
    if (!loading && !token) router.replace("/login");
  }, [token, loading, router]);

  const { updateJob, startPolling, removeJob, cancelLocalJob } = useJobPolling({
    token,
    jobs,
    setJobs,
    pollRefs,
    getJob: getCombineJob,
  });

  function handleFiles(incoming: FileList | null) {
    if (!incoming) return;
    const videoFiles = Array.from(incoming).filter((f) => f.type.startsWith("video/"));
    if (videoFiles.length === 0) { setError("Please drop video files only."); return; }
    setError("");
    setFiles((prev) => [...prev, ...videoFiles]);
  }

  function removeFile(index: number) {
    setFiles((prev) => prev.filter((_, i) => i !== index));
  }

  function moveFile(index: number, direction: -1 | 1) {
    setFiles((prev) => {
      const next = [...prev];
      const target = index + direction;
      if (target < 0 || target >= next.length) return prev;
      [next[index], next[target]] = [next[target], next[index]];
      return next;
    });
  }

  async function handleCombine() {
    if (!token || files.length < 2) return;
    setError("");

    const capturedFiles = [...files];
    const localId = createLocalId();
    const label = `${capturedFiles.length} clips`;

    const initialData: CombineJobData = {
      job_id: "",
      status: "queued",
      phase: "uploading",
      overall_progress: 0,
      clip_index: 0,
      total_clips: capturedFiles.length,
    };

    setJobs((prev) => [...prev, { localId, label, uploadProgress: 0, jobId: "", data: initialData }]);
    setFiles([]);

    try {
      const data = await startCombineJob(
        token,
        capturedFiles,
        (pct) => updateJob(localId, { uploadProgress: pct }),
      );
      updateJob(localId, { jobId: data.job_id, data, uploadProgress: 100 });
      startPolling(localId, data.job_id);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "Combine failed";
      updateJob(localId, { data: { ...initialData, status: "failed", error: msg } });
    }
  }

  function handleCancel(localId: string) {
    cancelLocalJob(localId, cancelCombineJob);
  }

  function handleDownload(item: CombineJobItem) {
    if (!item.data.result_url) return;
    void triggerFileDownload(item.data.result_url, "combined_video.mp4")
      .catch(() => setError("Failed to download combined video."));
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
        {/* Drop zone — drag only, matches Convert style */}
        <div className="glass-panel p-6 md:p-8 rounded-4xl mb-6 shadow-2xl shadow-purple-500/5">
          <div
            onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => { e.preventDefault(); setDragging(false); handleFiles(e.dataTransfer.files); }}
            className={`border-2 border-dashed rounded-3xl flex flex-col items-center justify-center gap-3 py-16 transition-all duration-300 ${
              dragging
                ? "border-indigo-400 bg-indigo-500/10"
                : "border-white/10 hover:border-indigo-500/50 hover:bg-white/5"
            }`}
          >
            <div className="w-14 h-14 rounded-full bg-indigo-500/15 flex items-center justify-center">
              <Film className="w-6 h-6 text-indigo-400" />
            </div>
            <p className="text-white font-medium">Drop 2 or more video clips here</p>
            <p className="text-gray-400 text-sm">Clips merge into a single 720p MP4 with crossfade transitions</p>
          </div>
        </div>

        {/* Pending file list + combine button */}
        {files.length > 0 && (
          <div className="glass-panel p-4 rounded-3xl mb-6 space-y-3">
            <p className="text-sm text-gray-400 px-1">
              {files.length} file{files.length !== 1 ? "s" : ""} selected — drag to reorder or use arrows
            </p>
            {files.map((f, i) => (
              <div key={i} className="flex items-center gap-2 px-1">
                <span className="text-xs text-gray-600 w-5 text-right shrink-0">{i + 1}</span>
                <div className="flex flex-col shrink-0">
                  <button
                    onClick={() => moveFile(i, -1)}
                    disabled={i === 0}
                    className="text-gray-600 hover:text-indigo-400 disabled:opacity-20 disabled:cursor-not-allowed transition-colors"
                  >
                    <ChevronUp className="w-3 h-3" />
                  </button>
                  <button
                    onClick={() => moveFile(i, 1)}
                    disabled={i === files.length - 1}
                    className="text-gray-600 hover:text-indigo-400 disabled:opacity-20 disabled:cursor-not-allowed transition-colors"
                  >
                    <ChevronDown className="w-3 h-3" />
                  </button>
                </div>
                <div className="flex items-center gap-3 min-w-0 flex-1">
                  <Film className="w-4 h-4 text-indigo-400 shrink-0" />
                  <span className="text-sm text-gray-200 truncate">{f.name}</span>
                  <span className="text-xs text-gray-500 shrink-0">
                    {(f.size / 1024 / 1024).toFixed(1)} MB
                  </span>
                </div>
                <button
                  onClick={() => removeFile(i)}
                  className="text-gray-500 hover:text-red-400 transition-colors shrink-0"
                >
                  <X className="w-4 h-4" />
                </button>
              </div>
            ))}
            {files.length === 1 && (
              <p className="text-center text-gray-500 text-sm py-1">Add at least one more video to combine.</p>
            )}
            {files.length >= 2 && (
              <>
                <Button
                  onClick={handleCombine}
                  className="w-full bg-indigo-600 hover:bg-indigo-500 text-white rounded-xl mt-2"
                >
                  <Film className="w-4 h-4 mr-2" />
                  Combine {files.length} Videos
                </Button>
                <p className="text-center text-gray-600 text-xs pt-1">
                  After submitting, drop more clips above to start another job
                </p>
              </>
            )}
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
                    <p className="text-sm font-medium text-white truncate">{item.label}</p>
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
                        ) : item.data.overall_progress > 0 ? (
                          <div
                            className="h-full bg-indigo-500 rounded-full transition-all duration-500"
                            style={{ width: `${item.data.overall_progress}%` }}
                          />
                        ) : isQueued ? (
                          <div className="h-full rounded-full animate-pulse w-full bg-yellow-500/60" />
                        ) : (
                          <div className="h-full w-full bg-linear-to-r from-indigo-500/0 via-indigo-500/60 to-indigo-500/0 animate-pulse rounded-full" />
                        )}
                      </div>
                    )}
                    {isDone && item.data.result_url && (
                      <ResultVideoPlayer src={item.data.result_url} title={item.label} />
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
