"use client";

import { useEffect, useRef, useState, useCallback } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "../auth-context";
import { startCombineJob, getCombineJob, cancelCombineJob, CombineJobData } from "../api";
import { Button } from "@/components/ui/button";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Loader2, Upload, X, Check, Download, Film } from "lucide-react";
import Navbar from "@/components/Navbar";

interface JobState {
  jobId: string;
  data: CombineJobData;
  uploadProgress: number;
}

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

export default function CombinePage() {
  const { token, loading, authEnabled } = useAuth();
  const router = useRouter();

  const [files, setFiles] = useState<File[]>([]);
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

  const handleFiles = useCallback((incoming: FileList | null) => {
    if (!incoming) return;
    const videoFiles = Array.from(incoming).filter((f) => f.type.startsWith("video/"));
    if (videoFiles.length === 0) {
      setError("Please select video files only.");
      return;
    }
    setError("");
    setFiles((prev) => [...prev, ...videoFiles]);
  }, []);

  function removeFile(index: number) {
    setFiles((prev) => prev.filter((_, i) => i !== index));
  }

  function startPolling(jobId: string) {
    if (pollRef.current) clearInterval(pollRef.current);
    pollRef.current = setInterval(async () => {
      if (!token) return;
      try {
        const data = await getCombineJob(token, jobId);
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

  async function handleCombine() {
    if (!token || files.length < 2) return;
    setError("");
    setJob(null);

    try {
      const data = await startCombineJob(
        token,
        files,
        (pct) => setJob((prev) => prev ? { ...prev, uploadProgress: pct } : {
          jobId: "",
          data: { job_id: "", status: "uploading", phase: "uploading", overall_progress: 0, clip_index: 0, total_clips: files.length },
          uploadProgress: pct,
        }),
      );
      setJob({ jobId: data.job_id, data, uploadProgress: 100 });
      startPolling(data.job_id);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "Combine failed";
      setError(msg);
    }
  }

  async function handleCancel() {
    if (!token || !job) return;
    try {
      await cancelCombineJob(token, job.jobId);
      if (pollRef.current) clearInterval(pollRef.current);
      setJob(null);
      setFiles([]);
    } catch {
      // ignore
    }
  }

  function handleDownload() {
    if (!job?.data.result_url) return;
    const a = document.createElement("a");
    a.href = job.data.result_url;
    a.download = "combined_video.mp4";
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
              <Film className="w-5 h-5 text-indigo-400" />
              Combine Videos
            </CardTitle>
            <p className="text-gray-400 text-sm">
              Drop 2 or more video files. They&apos;ll be merged in order with smooth crossfade transitions and exported as a single 720p MP4.
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
                  handleFiles(e.dataTransfer.files);
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
                  Drag &amp; drop video files here, or <span className="text-indigo-400 underline">browse</span>
                </p>
                <p className="text-gray-500 text-xs mt-1">Select multiple files at once</p>
                <input
                  ref={fileInputRef}
                  type="file"
                  accept="video/*"
                  multiple
                  className="hidden"
                  onChange={(e) => handleFiles(e.target.files)}
                />
              </div>
            )}

            {/* File list */}
            {files.length > 0 && !isProcessing && !isDone && (
              <div className="space-y-2">
                <p className="text-sm text-gray-400">{files.length} file{files.length > 1 ? "s" : ""} selected</p>
                {files.map((f, i) => (
                  <div
                    key={i}
                    className="glass-panel px-4 py-3 rounded-xl flex items-center justify-between gap-3"
                  >
                    <div className="flex items-center gap-3 min-w-0">
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
                      : phaseLabel(job.data.phase, job.data)}
                  </span>
                  <span className="text-gray-500">{job.data.overall_progress}%</span>
                </div>
                <div className="h-2 bg-white/10 rounded-full overflow-hidden">
                  <div
                    className="h-full bg-linear-to-r from-indigo-500 to-purple-500 rounded-full transition-all duration-500"
                    style={{ width: `${job.uploadProgress < 100 ? job.uploadProgress : job.data.overall_progress}%` }}
                  />
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
                  <span className="text-sm font-medium">Combine complete!</span>
                </div>
                <Button
                  onClick={handleDownload}
                  className="w-full bg-indigo-600 hover:bg-indigo-500 text-white rounded-xl"
                >
                  <Download className="w-4 h-4 mr-2" /> Download Combined Video
                </Button>
                <Button
                  variant="outline"
                  onClick={() => { setJob(null); setFiles([]); }}
                  className="w-full border-white/10 bg-transparent hover:bg-white/5 text-gray-300 rounded-xl"
                >
                  Combine more videos
                </Button>
              </div>
            )}

            {/* Failed */}
            {isFailed && (
              <div className="space-y-3">
                <p className="text-red-400 text-sm">
                  Combine failed: {job?.data.error || "Unknown error"}
                </p>
                <Button
                  variant="outline"
                  onClick={() => { setJob(null); setFiles([]); setError(""); }}
                  className="border-white/10 bg-transparent hover:bg-white/5 text-gray-300 rounded-xl"
                >
                  Try again
                </Button>
              </div>
            )}

            {/* Combine button */}
            {!isProcessing && !isDone && files.length >= 2 && (
              <Button
                onClick={handleCombine}
                disabled={files.length < 2}
                className="w-full bg-indigo-600 hover:bg-indigo-500 text-white rounded-xl disabled:opacity-40"
              >
                <Film className="w-4 h-4 mr-2" />
                Combine {files.length} Videos
              </Button>
            )}

            {!isProcessing && !isDone && files.length === 1 && (
              <p className="text-center text-gray-500 text-sm">Add at least one more video to combine.</p>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
