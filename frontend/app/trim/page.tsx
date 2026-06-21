"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "../auth-context";
import { useJobs, type TrimJobItem } from "../jobs-context";
import { createLocalId, triggerFileDownload, useJobPolling } from "../job-utils";
import { startTrimJob, getTrimJob, cancelTrimJob, TrimJobData } from "../api";
import { Button } from "@/components/ui/button";
import {
  Loader2, X, Check, Download, Scissors, Clock, Ban, Trash2, Play, Pause,
} from "lucide-react";
import Navbar from "@/components/Navbar";
import ResultVideoPlayer from "@/components/ResultVideoPlayer";

function formatTime(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

function parseTimeInput(str: string): number | null {
  const parts = str.split(":").map(Number);
  if (parts.some(isNaN)) return null;
  if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
  if (parts.length === 2) return parts[0] * 60 + parts[1];
  if (parts.length === 1 && !isNaN(parts[0])) return parts[0];
  return null;
}

function statusMessage(item: TrimJobItem): string {
  if (item.jobId === "") {
    return item.uploadProgress < 100 ? `Uploading… ${item.uploadProgress}%` : "Waiting for server…";
  }
  const { data } = item;
  if (data.status === "queued") return "Waiting for worker…";
  if (data.status === "processing") return `Trimming… ${data.progress}%`;
  if (data.status === "done") return "Done!";
  if (data.status === "failed") return data.error || "Failed";
  return "Processing…";
}

export default function TrimPage() {
  const { token, loading } = useAuth();
  const router = useRouter();

  const [phase, setPhase] = useState<"upload" | "editor">("upload");
  const [file, setFile] = useState<File | null>(null);
  const [objectUrl, setObjectUrl] = useState<string | null>(null);
  const [duration, setDuration] = useState(0);
  const [startTime, setStartTime] = useState(0);
  const [endTime, setEndTime] = useState(0);
  const [startInput, setStartInput] = useState("00:00");
  const [endInput, setEndInput] = useState("00:00");
  const [isPreviewing, setIsPreviewing] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState("");

  const videoRef = useRef<HTMLVideoElement>(null);
  const previewIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const { trimJobs: jobs, setTrimJobs: setJobs, trimPollRefs: pollRefs } = useJobs();

  useEffect(() => {
    if (!loading && !token) router.replace("/login");
  }, [token, loading, router]);

  const { updateJob, startPolling, removeJob, cancelLocalJob } = useJobPolling({
    token,
    jobs,
    setJobs,
    pollRefs,
    getJob: getTrimJob,
  });

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      if (previewIntervalRef.current) clearInterval(previewIntervalRef.current);
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [objectUrl]);

  function handleFile(f: File) {
    if (!f.type.startsWith("video/")) { setError("Please select a video file."); return; }
    setError("");
    if (objectUrl) URL.revokeObjectURL(objectUrl);
    const url = URL.createObjectURL(f);
    setFile(f);
    setObjectUrl(url);
    setPhase("editor");
  }

  function handleDrop(e: React.DragEvent) {
    e.preventDefault();
    setDragging(false);
    const f = e.dataTransfer.files[0];
    if (f) handleFile(f);
  }

  function handleVideoLoaded() {
    const vid = videoRef.current;
    if (!vid) return;
    const d = vid.duration;
    setDuration(d);
    setStartTime(0);
    setEndTime(d);
    setStartInput(formatTime(0));
    setEndInput(formatTime(d));
  }

  function onStartSlider(val: number) {
    const clamped = Math.min(val, endTime - 0.1);
    setStartTime(clamped);
    setStartInput(formatTime(clamped));
  }

  function onEndSlider(val: number) {
    const clamped = Math.max(val, startTime + 0.1);
    setEndTime(clamped);
    setEndInput(formatTime(clamped));
  }

  function onStartInputBlur() {
    const parsed = parseTimeInput(startInput);
    if (parsed === null || parsed < 0 || parsed >= endTime) {
      setStartInput(formatTime(startTime));
      return;
    }
    setStartTime(parsed);
    setStartInput(formatTime(parsed));
  }

  function onEndInputBlur() {
    const parsed = parseTimeInput(endInput);
    if (parsed === null || parsed > duration || parsed <= startTime) {
      setEndInput(formatTime(endTime));
      return;
    }
    setEndTime(parsed);
    setEndInput(formatTime(parsed));
  }

  function setStartToCurrent() {
    if (!videoRef.current) return;
    const t = Math.min(videoRef.current.currentTime, endTime - 0.1);
    setStartTime(t);
    setStartInput(formatTime(t));
  }

  function setEndToCurrent() {
    if (!videoRef.current) return;
    const t = Math.max(videoRef.current.currentTime, startTime + 0.1);
    setEndTime(t);
    setEndInput(formatTime(t));
  }

  function handlePreview() {
    if (!videoRef.current) return;
    if (isPreviewing) {
      videoRef.current.pause();
      if (previewIntervalRef.current) clearInterval(previewIntervalRef.current);
      previewIntervalRef.current = null;
      setIsPreviewing(false);
      return;
    }
    videoRef.current.currentTime = startTime;
    videoRef.current.play();
    setIsPreviewing(true);
    const interval = setInterval(() => {
      if (!videoRef.current) { clearInterval(interval); return; }
      if (videoRef.current.currentTime >= endTime) {
        videoRef.current.pause();
        clearInterval(interval);
        previewIntervalRef.current = null;
        setIsPreviewing(false);
      }
    }, 100);
    previewIntervalRef.current = interval;
  }

  async function handleTrim() {
    if (!token || !file || endTime <= startTime) return;
    setError("");

    const capturedFile = file;
    const localId = createLocalId();

    const initialData: TrimJobData = { job_id: "", status: "queued", progress: 0 };

    setJobs((prev) => [
      ...prev,
      { localId, filename: capturedFile.name, uploadProgress: 0, jobId: "", data: initialData },
    ]);

    try {
      const data = await startTrimJob(
        token,
        capturedFile,
        startTime,
        endTime,
        (pct) => updateJob(localId, { uploadProgress: pct }),
      );
      updateJob(localId, { jobId: data.job_id, data, uploadProgress: 100 });
      startPolling(localId, data.job_id);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "Trim failed";
      updateJob(localId, { data: { ...initialData, status: "failed", error: msg } });
    }
  }

  function handleCancel(localId: string) {
    cancelLocalJob(localId, cancelTrimJob);
  }

  function handleDownload(item: TrimJobItem) {
    if (!item.data.result_url) return;
    void triggerFileDownload(item.data.result_url, `trimmed_${item.filename}`)
      .catch(() => setError("Failed to download trimmed video."));
  }

  function handleDelete(localId: string) {
    removeJob(localId);
  }

  function handleChangeVideo() {
    setPhase("upload");
    setIsPreviewing(false);
    if (previewIntervalRef.current) { clearInterval(previewIntervalRef.current); previewIntervalRef.current = null; }
    if (objectUrl) URL.revokeObjectURL(objectUrl);
    setObjectUrl(null);
    setFile(null);
    setDuration(0);
  }

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center text-white">
        <Loader2 className="animate-spin w-8 h-8 text-indigo-400" />
      </div>
    );
  }

  const startPct = duration > 0 ? startTime / duration : 0;
  const endPct = duration > 0 ? endTime / duration : 1;
  // When start handle is near the right edge, bring it on top so it stays clickable
  const startOnTop = startPct > 0.9;

  return (
    <div className="min-h-screen text-white pb-20">
      <Navbar />

      <div className="max-w-3xl mx-auto px-4 sm:px-6">
        {phase === "upload" ? (
          <div className="glass-panel p-6 md:p-8 rounded-4xl mb-8 shadow-2xl shadow-purple-500/5">
            <input
              ref={fileInputRef}
              type="file"
              accept="video/*"
              className="hidden"
              onChange={(e) => { const f = e.target.files?.[0]; if (f) handleFile(f); }}
            />
            <div
              onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
              onDragLeave={() => setDragging(false)}
              onDrop={handleDrop}
              onClick={() => fileInputRef.current?.click()}
              className={`border-2 border-dashed rounded-3xl flex flex-col items-center justify-center gap-3 py-16 cursor-pointer transition-all duration-300 ${
                dragging
                  ? "border-indigo-400 bg-indigo-500/10"
                  : "border-white/10 hover:border-indigo-500/50 hover:bg-white/5"
              }`}
            >
              <div className="w-14 h-14 rounded-full bg-indigo-500/15 flex items-center justify-center">
                <Scissors className="w-6 h-6 text-indigo-400" />
              </div>
              <p className="text-white font-medium">Drop a video here or click to browse</p>
              <p className="text-gray-400 text-sm">Select start and end points to trim</p>
            </div>
            {error && <p className="text-red-400 text-sm mt-3">{error}</p>}
          </div>
        ) : (
          <div className="glass-panel p-6 rounded-4xl mb-6 shadow-2xl shadow-purple-500/5">
            {/* Video player */}
            <video
              ref={videoRef}
              src={objectUrl ?? ""}
              onLoadedMetadata={handleVideoLoaded}
              controls
              className="w-full rounded-2xl mb-5 bg-black"
            />

            {/* Set Start / Set End buttons */}
            <div className="flex justify-between mb-3">
              <button
                onClick={setStartToCurrent}
                className="text-xs text-indigo-400 hover:text-indigo-300 transition-colors"
              >
                Set Start
              </button>
              <button
                onClick={setEndToCurrent}
                className="text-xs text-indigo-400 hover:text-indigo-300 transition-colors"
              >
                Set End
              </button>
            </div>

            {/* Dual-handle range slider */}
            <div className="relative h-6 flex items-center mb-3">
              {/* Track background */}
              <div className="absolute w-full h-1.5 rounded-full bg-white/10" />
              {/* Selected range highlight */}
              <div
                className="absolute h-1.5 bg-indigo-500 rounded-full"
                style={{
                  left: `${startPct * 100}%`,
                  width: `${(endPct - startPct) * 100}%`,
                }}
              />
              {/* Start range input (transparent, full width) */}
              <input
                type="range"
                min={0}
                max={duration}
                step={0.1}
                value={startTime}
                onChange={(e) => onStartSlider(Number(e.target.value))}
                className="absolute w-full h-full opacity-0 cursor-pointer"
                style={{ zIndex: startOnTop ? 20 : 10 }}
              />
              {/* End range input (transparent, full width) */}
              <input
                type="range"
                min={0}
                max={duration}
                step={0.1}
                value={endTime}
                onChange={(e) => onEndSlider(Number(e.target.value))}
                className="absolute w-full h-full opacity-0 cursor-pointer"
                style={{ zIndex: startOnTop ? 10 : 20 }}
              />
              {/* Start handle dot */}
              <div
                className="absolute w-4 h-4 rounded-full bg-white border-2 border-indigo-400 shadow-md pointer-events-none"
                style={{ left: `${startPct * 100}%`, transform: "translateX(-50%)", zIndex: 30 }}
              />
              {/* End handle dot */}
              <div
                className="absolute w-4 h-4 rounded-full bg-white border-2 border-indigo-400 shadow-md pointer-events-none"
                style={{ left: `${endPct * 100}%`, transform: "translateX(-50%)", zIndex: 30 }}
              />
            </div>

            {/* Time inputs */}
            <div className="flex justify-between mb-5">
              <input
                type="text"
                value={startInput}
                onChange={(e) => setStartInput(e.target.value)}
                onBlur={onStartInputBlur}
                className="w-24 text-center bg-white/5 border border-white/10 rounded-lg px-2 py-1 text-sm text-white focus:outline-none focus:border-indigo-500"
              />
              <input
                type="text"
                value={endInput}
                onChange={(e) => setEndInput(e.target.value)}
                onBlur={onEndInputBlur}
                className="w-24 text-center bg-white/5 border border-white/10 rounded-lg px-2 py-1 text-sm text-white focus:outline-none focus:border-indigo-500"
              />
            </div>

            {/* Action buttons */}
            <div className="flex gap-3">
              <Button
                onClick={handlePreview}
                variant="outline"
                className="flex-1 border-white/10 bg-white/5 hover:bg-white/10 text-white rounded-xl"
              >
                {isPreviewing ? (
                  <><Pause className="w-4 h-4 mr-2" />Stop</>
                ) : (
                  <><Play className="w-4 h-4 mr-2" />Preview</>
                )}
              </Button>
              <Button
                onClick={handleTrim}
                className="flex-1 bg-indigo-600 hover:bg-indigo-500 text-white rounded-xl"
              >
                <Scissors className="w-4 h-4 mr-2" />
                Trim
              </Button>
            </div>

            <button
              onClick={handleChangeVideo}
              className="mt-3 text-xs text-gray-500 hover:text-gray-300 transition-colors w-full text-center"
            >
              ← Change video
            </button>
          </div>
        )}

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
