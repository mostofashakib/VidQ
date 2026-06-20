"use client";

import { useEffect, useRef, useState, useCallback } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "../auth-context";
import { startTranslateJob, getTranslateJob, cancelTranslateJob, TranslateJobData } from "../api";
import { Button } from "@/components/ui/button";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Loader2, Upload, X, Check, Download, Captions } from "lucide-react";
import Navbar from "@/components/Navbar";

interface JobState {
  jobId: string;
  data: TranslateJobData;
  uploadProgress: number;
}

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

export default function TranslatePage() {
  const { token, loading } = useAuth();
  const router = useRouter();

  const [file, setFile] = useState<File | null>(null);
  const [dragging, setDragging] = useState(false);
  const [job, setJob] = useState<JobState | null>(null);
  const [error, setError] = useState("");
  const fileInputRef = useRef<HTMLInputElement>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    if (!loading && !token) router.replace("/login");
  }, [token, loading, router]);

  useEffect(() => {
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, []);

  const handleFile = useCallback((incoming: FileList | null) => {
    if (!incoming || incoming.length === 0) return;
    const f = incoming[0];
    if (!f.type.startsWith("video/")) {
      setError("Please select a video file.");
      return;
    }
    setError("");
    setFile(f);
  }, []);

  function startPolling(jobId: string) {
    if (pollRef.current) clearInterval(pollRef.current);
    pollRef.current = setInterval(async () => {
      if (!token) return;
      try {
        const data = await getTranslateJob(token, jobId);
        setJob((prev) => prev ? { ...prev, data } : null);
        if (data.status === "done" || data.status === "failed" || data.status === "cancelled") {
          clearInterval(pollRef.current!);
          pollRef.current = null;
        }
      } catch {
        // ignore transient errors
      }
    }, 2000);
  }

  async function handleTranslate() {
    if (!token || !file) return;
    setError("");
    setJob(null);

    try {
      const data = await startTranslateJob(
        token,
        file,
        (pct) => setJob((prev) => prev ? { ...prev, uploadProgress: pct } : {
          jobId: "",
          data: {
            job_id: "", filename: file.name, status: "uploading",
            phase: "uploading", overall_progress: 0, chunk_index: 0, total_chunks: 0,
          },
          uploadProgress: pct,
        }),
      );
      setJob({ jobId: data.job_id, data, uploadProgress: 100 });
      startPolling(data.job_id);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "Translate failed";
      setError(msg);
    }
  }

  async function handleCancel() {
    if (!token || !job) return;
    try {
      await cancelTranslateJob(token, job.jobId);
      if (pollRef.current) clearInterval(pollRef.current);
      setJob(null);
      setFile(null);
    } catch {
      // ignore
    }
  }

  function handleDownload() {
    if (!job?.data.result_url) return;
    const a = document.createElement("a");
    a.href = job.data.result_url;
    a.download = "subtitled_video.mp4";
    a.click();
  }

  const isDone = job?.data.status === "done";
  const isFailed = job?.data.status === "failed";
  const isProcessing = job && !isDone && !isFailed && job.data.status !== "cancelled";

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
        <Card className="glass-panel rounded-4xl border-white/10 shadow-2xl shadow-purple-500/5 mb-8">
          <CardHeader>
            <CardTitle className="text-white flex items-center gap-2">
              <Captions className="w-5 h-5 text-indigo-400" />
              Translate Video
            </CardTitle>
            <p className="text-gray-400 text-sm">
              Drop a video file. English subtitles will be transcribed via Whisper, translated with a local LLM,
              and burned in YouTube-style at the bottom of the video.
            </p>
          </CardHeader>
          <CardContent className="space-y-6">
            {/* Drop zone */}
            {!isProcessing && !isDone && (
              <div
                onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
                onDragLeave={() => setDragging(false)}
                onDrop={(e) => {
                  e.preventDefault();
                  setDragging(false);
                  handleFile(e.dataTransfer.files);
                }}
                onClick={() => fileInputRef.current?.click()}
                className={`border-2 border-dashed rounded-2xl p-10 text-center cursor-pointer transition-all ${
                  dragging
                    ? "border-indigo-400 bg-indigo-500/10"
                    : "border-white/20 hover:border-indigo-400/50 hover:bg-white/5"
                }`}
              >
                <Upload className="mx-auto mb-3 w-8 h-8 text-indigo-400 opacity-70" />
                <p className="text-gray-300 text-sm">
                  Drag &amp; drop a video file here, or <span className="text-indigo-400 underline">browse</span>
                </p>
                <p className="text-gray-500 text-xs mt-1">Any video format supported</p>
                <input
                  ref={fileInputRef}
                  type="file"
                  accept="video/*"
                  className="hidden"
                  onChange={(e) => handleFile(e.target.files)}
                />
              </div>
            )}

            {/* Selected file */}
            {file && !isProcessing && !isDone && (
              <div className="glass-panel px-4 py-3 rounded-xl flex items-center justify-between gap-3">
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
            )}

            {error && (
              <p className="text-red-400 text-sm">{error}</p>
            )}

            {/* Processing progress */}
            {isProcessing && job && (
              <div className="space-y-3">
                <div className="flex items-center justify-between text-sm">
                  <span className="text-gray-300">
                    {job.data.status === "uploading" || job.uploadProgress < 100
                      ? `Uploading… ${job.uploadProgress}%`
                      : job.data.status === "queued"
                      ? "Queued, waiting for worker…"
                      : phaseLabel(job.data)}
                  </span>
                  <span className="text-gray-500">{job.data.overall_progress}%</span>
                </div>
                <div className="h-2 bg-white/10 rounded-full overflow-hidden">
                  <div
                    className="h-full bg-linear-to-r from-indigo-500 to-purple-500 rounded-full transition-all duration-500"
                    style={{ width: `${job.uploadProgress < 100 ? job.uploadProgress : job.data.overall_progress}%` }}
                  />
                </div>
                {/* Phase indicators */}
                <div className="flex items-center gap-1 text-xs text-gray-500 mt-1">
                  {["extracting_audio", "transcribing", "translating", "burning"].map((ph, i) => {
                    const phases = ["extracting_audio", "transcribing", "translating", "burning"];
                    const currentIdx = phases.indexOf(job.data.phase);
                    const done = i < currentIdx;
                    const active = i === currentIdx;
                    return (
                      <span key={ph} className="flex items-center gap-1">
                        {i > 0 && <span className="text-gray-700">→</span>}
                        <span className={done ? "text-green-400" : active ? "text-indigo-400" : "text-gray-600"}>
                          {ph === "extracting_audio" ? "Audio" : ph === "transcribing" ? "Whisper" : ph === "translating" ? "LLM" : "Burn"}
                        </span>
                      </span>
                    );
                  })}
                </div>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={handleCancel}
                  className="border-white/10 bg-transparent hover:bg-red-500/10 hover:text-red-400 hover:border-red-500/30 text-gray-400 rounded-xl"
                >
                  <X className="w-3 h-3 mr-1" /> Cancel
                </Button>
              </div>
            )}

            {/* Done */}
            {isDone && (
              <div className="space-y-4">
                <div className="flex items-center gap-2 text-green-400">
                  <Check className="w-5 h-5" />
                  <span className="text-sm font-medium">Translation complete!</span>
                </div>
                <Button
                  onClick={handleDownload}
                  className="w-full bg-indigo-600 hover:bg-indigo-500 text-white rounded-xl"
                >
                  <Download className="w-4 h-4 mr-2" /> Download Subtitled Video
                </Button>
                <Button
                  variant="outline"
                  onClick={() => { setJob(null); setFile(null); }}
                  className="w-full border-white/10 bg-transparent hover:bg-white/5 text-gray-300 rounded-xl"
                >
                  Translate another video
                </Button>
              </div>
            )}

            {/* Failed */}
            {isFailed && (
              <div className="space-y-3">
                <p className="text-red-400 text-sm">
                  Translation failed: {job?.data.error || "Unknown error"}
                </p>
                <Button
                  variant="outline"
                  onClick={() => { setJob(null); setFile(null); setError(""); }}
                  className="border-white/10 bg-transparent hover:bg-white/5 text-gray-300 rounded-xl"
                >
                  Try again
                </Button>
              </div>
            )}

            {/* Translate button */}
            {!isProcessing && !isDone && file && (
              <Button
                onClick={handleTranslate}
                className="w-full bg-indigo-600 hover:bg-indigo-500 text-white rounded-xl"
              >
                <Captions className="w-4 h-4 mr-2" />
                Translate &amp; Add Captions
              </Button>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
