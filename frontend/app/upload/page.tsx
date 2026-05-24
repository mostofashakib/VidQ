"use client";

import { useEffect, useRef, useState, useCallback } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "../auth-context";
import {
  uploadVideo, getUploadJob, cancelUploadJob,
  listUploadedVideos, deleteVideo, downloadVideo,
} from "../api";
import { Button } from "@/components/ui/button";
import {
  Card, CardHeader, CardTitle, CardContent, CardFooter,
} from "@/components/ui/card";
import {
  Dialog, DialogContent, DialogHeader, DialogFooter, DialogTitle,
} from "@/components/ui/dialog";
import { Trash, Download, Check, X, Loader2, Upload, Ban, Clock } from "lucide-react";
import Link from "next/link";

interface UploadedVideo {
  id: number;
  url: string;
  category: string;
  title?: string;
  duration?: number;
  source: string;
  created_at: string;
}

interface UploadJob {
  localId: string;
  filename: string;
  /** uploading = HTTP transfer; queued = waiting for a worker slot; processing = ffmpeg running */
  status: "uploading" | "queued" | "processing" | "done" | "failed" | "cancelled";
  message: string;
  progress: number;
  jobId?: string;
}

export default function UploadPage() {
  const { token, loading, authEnabled, logout } = useAuth();
  const router = useRouter();

  const [videos, setVideos] = useState<UploadedVideo[]>([]);
  const [jobs, setJobs] = useState<UploadJob[]>([]);
  const [error, setError] = useState("");
  const [deleteId, setDeleteId] = useState<number | null>(null);
  const [showDialog, setShowDialog] = useState(false);
  const [dragging, setDragging] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Maps localId → abort function for the current phase
  const abortRefs = useRef<Map<string, () => void>>(new Map());
  // Maps localId → polling interval id
  const pollRefs = useRef<Map<string, ReturnType<typeof setInterval>>>(new Map());

  useEffect(() => {
    if (!loading && !token) router.replace("/login");
  }, [token, loading, router]);

  useEffect(() => {
    if (!token) return;
    listUploadedVideos(token)
      .then(setVideos)
      .catch(() => setError("Failed to load uploaded videos"));
  }, [token]);

  // Clean up polling intervals on unmount
  useEffect(() => {
    return () => {
      pollRefs.current.forEach((id) => clearInterval(id));
    };
  }, []);

  function formatDuration(seconds?: number) {
    if (!seconds || isNaN(seconds)) return "Unknown";
    const m = Math.floor(seconds / 60);
    const s = Math.round(seconds % 60);
    return `${m}:${s.toString().padStart(2, "0")}`;
  }

  function updateJob(localId: string, patch: Partial<UploadJob>) {
    setJobs((prev) => prev.map((j) => j.localId === localId ? { ...j, ...patch } : j));
  }

  function startPolling(localId: string, jobId: string) {
    const intervalId = setInterval(async () => {
      try {
        const data = await getUploadJob(token!, jobId);

        if (data.status === "queued") {
          updateJob(localId, { status: "queued", message: "Waiting for worker…" });
        } else if (data.status === "processing") {
          updateJob(localId, { status: "processing", message: "Scaling to 720p…" });
        } else if (data.status === "done") {
          clearInterval(intervalId);
          pollRefs.current.delete(localId);
          abortRefs.current.delete(localId);
          updateJob(localId, { status: "done", message: "Done! Scaled to 720p." });
          listUploadedVideos(token!).then(setVideos).catch(() => {});
          setTimeout(() => {
            setJobs((prev) => prev.filter((j) => j.localId !== localId));
          }, 4000);
        } else if (data.status === "failed") {
          clearInterval(intervalId);
          pollRefs.current.delete(localId);
          abortRefs.current.delete(localId);
          updateJob(localId, { status: "failed", message: data.error || "Processing failed." });
        } else if (data.status === "cancelled") {
          clearInterval(intervalId);
          pollRefs.current.delete(localId);
          abortRefs.current.delete(localId);
          setJobs((prev) => prev.filter((j) => j.localId !== localId));
        }
      } catch {
        // transient poll error — keep trying
      }
    }, 2000);

    pollRefs.current.set(localId, intervalId);
  }

  async function handleFiles(files: FileList | null) {
    if (!files || !files.length) return;
    setError("");

    // Kick off all uploads in parallel — each is independent
    Array.from(files).forEach((file) => {
      const localId = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
      setJobs((prev) => [
        ...prev,
        { localId, filename: file.name, status: "uploading", message: "Uploading…", progress: 0 },
      ]);
      runUpload(localId, file);
    });
  }

  async function runUpload(localId: string, file: File) {
    const controller = new AbortController();
    abortRefs.current.set(localId, () => controller.abort());

    try {
      const jobData = await uploadVideo(
        token!,
        file,
        (pct) => {
          updateJob(localId, {
            progress: pct,
            message: pct < 100 ? `Uploading… ${pct}%` : "Waiting for server…",
          });
        },
        controller.signal,
      );

      // Switch abort to cancel the server-side ffmpeg job
      abortRefs.current.set(localId, () => {
        cancelUploadJob(token!, jobData.job_id).catch(() => {});
      });

      updateJob(localId, {
        status: jobData.status as "queued" | "processing",
        message: jobData.status === "queued" ? "Waiting for worker…" : "Scaling to 720p…",
        progress: 100,
        jobId: jobData.job_id,
      });

      startPolling(localId, jobData.job_id);

    } catch (err: unknown) {
      // Abort throws a CanceledError — treat that as cancelled, not failure
      const isCancelled =
        err && typeof err === "object" && "code" in err && (err as { code: string }).code === "ERR_CANCELED";
      if (!isCancelled) {
        updateJob(localId, { status: "failed", message: "Upload failed." });
      } else {
        setJobs((prev) => prev.filter((j) => j.localId !== localId));
      }
      abortRefs.current.delete(localId);
    }
  }

  function handleCancel(localId: string) {
    // Stop polling if in processing phase
    const interval = pollRefs.current.get(localId);
    if (interval) {
      clearInterval(interval);
      pollRefs.current.delete(localId);
    }
    // Abort the current phase (HTTP upload or ffmpeg job)
    const abort = abortRefs.current.get(localId);
    if (abort) {
      abort();
      abortRefs.current.delete(localId);
    }
    setJobs((prev) => prev.filter((j) => j.localId !== localId));
  }

  function onInputChange(e: React.ChangeEvent<HTMLInputElement>) {
    handleFiles(e.target.files);
    e.target.value = "";
  }

  function onDrop(e: React.DragEvent) {
    e.preventDefault();
    setDragging(false);
    handleFiles(e.dataTransfer.files);
  }

  async function confirmDelete() {
    if (deleteId == null) return;
    try {
      await deleteVideo(token!, deleteId);
      setVideos((prev) => prev.filter((v) => v.id !== deleteId));
      setDeleteId(null);
      setShowDialog(false);
    } catch {
      setError("Failed to delete video");
    }
  }

  if (loading) {
    return <div className="min-h-screen flex items-center justify-center">Loading…</div>;
  }

  return (
    <div className="min-h-screen text-white pb-20">
      {/* Header */}
      <div className="flex justify-between items-center px-8 py-5 glass-panel sticky top-0 z-50 rounded-b-2xl mx-4 mb-10 shadow-xl shadow-indigo-500/10">
        <div className="flex items-center gap-4">
          <Link href="/">
            <span className="text-2xl font-bold bg-clip-text text-transparent bg-linear-to-r from-indigo-400 to-purple-400 cursor-pointer">
              VidQ
            </span>
          </Link>
          <span className="text-gray-500 text-sm hidden sm:inline">/ Upload</span>
        </div>
        <div className="flex items-center gap-3">
          <Link href="/">
            <Button variant="outline" className="border-white/10 bg-transparent hover:bg-white/10 hover:text-white transition-all rounded-xl text-gray-200 text-sm">
              ← Library
            </Button>
          </Link>
          {authEnabled && (
            <Button variant="outline" onClick={logout} className="border-white/10 bg-transparent hover:bg-white/10 hover:text-white transition-all rounded-xl text-gray-200">
              Logout
            </Button>
          )}
        </div>
      </div>

      <div className="max-w-6xl mx-auto px-4 sm:px-6">
        {/* Drop zone */}
        <div className="glass-panel p-6 md:p-8 rounded-4xl mb-8 shadow-2xl shadow-purple-500/5">
          <div
            onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
            onDragLeave={() => setDragging(false)}
            onDrop={onDrop}
            onClick={() => fileInputRef.current?.click()}
            className={`border-2 border-dashed rounded-3xl flex flex-col items-center justify-center gap-3 py-16 cursor-pointer transition-all duration-300 ${
              dragging
                ? "border-indigo-400 bg-indigo-500/10"
                : "border-white/10 hover:border-indigo-500/50 hover:bg-white/5"
            }`}
          >
            <div className="w-14 h-14 rounded-full bg-indigo-500/15 flex items-center justify-center">
              <Upload className="w-6 h-6 text-indigo-400" />
            </div>
            <p className="text-white font-medium">Drop video files here</p>
            <p className="text-gray-400 text-sm">or click to browse — any resolution, auto-scaled to 720p</p>
            <input
              ref={fileInputRef}
              type="file"
              accept="video/*"
              multiple
              className="hidden"
              onChange={onInputChange}
            />
          </div>
        </div>

        {error && <div className="text-red-400 mb-4">{error}</div>}

        {/* Active upload / processing cards */}
        {jobs.length > 0 && (
          <div className="mb-10 space-y-2.5">
            {jobs.map((job) => (
              <div
                key={job.localId}
                className={`glass-panel px-5 py-4 rounded-2xl border flex items-center gap-4 transition-all ${
                  job.status === "done"
                    ? "border-green-500/25"
                    : job.status === "failed"
                    ? "border-red-500/25"
                    : "border-indigo-500/20"
                }`}
              >
                <div className="shrink-0">
                  {job.status === "done" ? (
                    <div className="w-8 h-8 rounded-full bg-green-500/15 flex items-center justify-center">
                      <Check className="w-4 h-4 text-green-400" />
                    </div>
                  ) : job.status === "failed" ? (
                    <div className="w-8 h-8 rounded-full bg-red-500/15 flex items-center justify-center">
                      <X className="w-4 h-4 text-red-400" />
                    </div>
                  ) : job.status === "queued" ? (
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
                  <p className="text-sm font-medium text-white truncate">{job.filename}</p>
                  <p className={`text-xs mt-0.5 ${
                    job.status === "done" ? "text-green-400"
                    : job.status === "failed" ? "text-red-400"
                    : job.status === "queued" ? "text-yellow-400"
                    : "text-indigo-400"
                  }`}>
                    {job.message}
                  </p>
                  {job.status === "uploading" && job.progress > 0 && job.progress < 100 && (
                    <div className="mt-1.5 h-1 bg-white/10 rounded-full overflow-hidden">
                      <div
                        className="h-full bg-indigo-500 rounded-full transition-all duration-300"
                        style={{ width: `${job.progress}%` }}
                      />
                    </div>
                  )}
                  {(job.status === "processing" || job.status === "queued") && (
                    <div className="mt-1.5 h-1 bg-white/10 rounded-full overflow-hidden">
                      <div className={`h-full rounded-full animate-pulse w-full ${job.status === "queued" ? "bg-yellow-500/60" : "bg-indigo-500/60"}`} />
                    </div>
                  )}
                </div>

                {/* Cancel button for active jobs */}
                {(job.status === "uploading" || job.status === "queued" || job.status === "processing") && (
                  <button
                    title="Cancel"
                    onClick={() => handleCancel(job.localId)}
                    className="shrink-0 text-gray-500 hover:text-red-400 transition-colors"
                  >
                    <Ban className="w-4 h-4" />
                  </button>
                )}

                {/* Dismiss button for finished/failed jobs */}
                {(job.status === "done" || job.status === "failed") && (
                  <button
                    onClick={() => setJobs((prev) => prev.filter((j) => j.localId !== job.localId))}
                    className="shrink-0 text-gray-500 hover:text-white transition-colors"
                  >
                    <X className="w-4 h-4" />
                  </button>
                )}
              </div>
            ))}
          </div>
        )}

        {/* Completed video grid */}
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-8">
          {videos.length === 0 && jobs.length === 0 && (
            <div className="text-gray-500 col-span-3">No uploaded videos yet.</div>
          )}
          {videos.map((video) => (
            <Card
              key={video.id}
              className="glass-panel overflow-hidden flex flex-col min-h-90 min-w-[320px] rounded-3xl border border-white/10 hover:border-indigo-500/30 shadow-xl hover:shadow-indigo-500/20 transform transition-all hover:-translate-y-2 duration-500 group bg-transparent"
            >
              <CardHeader className="pb-3 pt-5 relative z-10">
                <CardTitle
                  className="text-lg font-bold wrap-break-word whitespace-pre-line leading-tight truncate text-white drop-shadow-md"
                  title={video.title}
                >
                  {video.title || "Uploaded Video"}
                </CardTitle>
              </CardHeader>
              <CardContent className="flex-1 flex flex-col justify-between z-10 px-6">
                <div className="mb-5 text-xs flex gap-2">
                  {video.duration !== undefined && video.duration !== null && (
                    <span className="inline-block px-3 py-1.5 bg-black/30 rounded-full text-gray-300 font-mono text-[10px] border border-white/5">
                      {formatDuration(video.duration)}
                    </span>
                  )}
                  <span className="inline-block px-3 py-1.5 bg-purple-500/10 rounded-full text-purple-300 font-medium tracking-wider uppercase text-[10px] border border-purple-500/10">
                    720p
                  </span>
                </div>
                <div className="rounded-xl overflow-hidden shadow-inner bg-black/40 aspect-video">
                  <video
                    className="w-full h-full object-cover opacity-70 group-hover:opacity-100 transition-opacity duration-300"
                    controls
                    preload="metadata"
                  >
                    <source src={video.url} type="video/mp4" />
                    <source src={video.url} type="video/webm" />
                  </video>
                </div>
              </CardContent>
              <CardFooter className="flex justify-end gap-2 pt-0 pb-5 pr-6 z-10">
                <Button
                  variant="outline"
                  size="icon"
                  title="Download"
                  onClick={async (e) => {
                    e.stopPropagation();
                    try {
                      const blob = await downloadVideo(token!, video.id);
                      const blobUrl = window.URL.createObjectURL(blob);
                      const a = document.createElement("a");
                      a.href = blobUrl;
                      const ext = video.url.split(".").pop()?.split("?")[0] || "mp4";
                      a.download = video.title
                        ? `${video.title.replace(/[^a-z0-9]/gi, "_").toLowerCase()}_720p.${ext}`
                        : `upload-${video.id}.${ext}`;
                      document.body.appendChild(a);
                      a.click();
                      document.body.removeChild(a);
                      window.URL.revokeObjectURL(blobUrl);
                    } catch {
                      window.open(video.url, "_blank");
                    }
                  }}
                  className="h-10 w-10 p-0 rounded-full bg-indigo-500/10 text-indigo-400 hover:bg-indigo-500 hover:text-white border border-indigo-500/20 transition-all shadow hover:shadow-indigo-500/40"
                >
                  <Download className="w-4 h-4" />
                </Button>
                <Button
                  variant="destructive"
                  size="icon"
                  onClick={(e) => {
                    e.stopPropagation();
                    setDeleteId(video.id);
                    setShowDialog(true);
                  }}
                  className="h-10 w-10 p-0 rounded-full bg-red-500/10 text-red-400 hover:bg-red-500 hover:text-white border border-red-500/20 transition-all shadow hover:shadow-red-500/40"
                >
                  <Trash className="w-4 h-4" />
                </Button>
              </CardFooter>
            </Card>
          ))}
        </div>
      </div>

      {/* Delete confirmation */}
      <Dialog open={showDialog} onOpenChange={setShowDialog}>
        <DialogContent className="glass-panel border-white/10 bg-gray-950/90 text-white rounded-4xl p-6 sm:p-8 shadow-2xl shadow-black">
          <DialogHeader>
            <DialogTitle className="text-xl">Delete Video</DialogTitle>
          </DialogHeader>
          <div className="text-gray-300 my-2">
            Are you sure? The file will be permanently removed.
          </div>
          <DialogFooter className="mt-4 gap-2">
            <Button
              variant="outline"
              onClick={() => setShowDialog(false)}
              className="rounded-xl border-white/10 hover:bg-white/10 hover:text-white text-gray-300"
            >
              Cancel
            </Button>
            <Button
              onClick={confirmDelete}
              className="bg-red-500/20 text-red-400 hover:bg-red-500/40 hover:text-white border border-red-500/30 transition-all rounded-xl shadow-lg shadow-red-500/20"
            >
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
