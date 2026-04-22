"use client";

import { useEffect, useState, useRef, useCallback } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "./auth-context";
import {
  addVideo,
  listVideos,
  listCategories,
  deleteVideo,
  extractVideo,
  getQueueStatus,
  cancelJob,
  downloadVideo,
} from "./api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogFooter,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Card,
  CardHeader,
  CardTitle,
  CardContent,
  CardFooter,
} from "@/components/ui/card";
import Image from "next/image";
import { Trash, Download, Check, X, Loader2 } from "lucide-react";

interface Video {
  id: number;
  url: string;
  category: string;
  title?: string;
  created_at: string;
  duration?: number;
  thumbnail?: string;
}

interface DownloadJob {
  localId: string;
  url: string;
  title?: string;
  status: "extracting" | "adding" | "queued" | "processing" | "done" | "failed" | "cancelled";
  message: string;
  jobId?: string;
  queuePosition?: number;
  abortController?: AbortController;
}

const PAGE_SIZE = 20;

const STATUS_STYLES: Record<DownloadJob["status"], string> = {
  extracting: "bg-yellow-500/20 text-yellow-400",
  adding:     "bg-indigo-500/20 text-indigo-400",
  queued:     "bg-orange-500/20 text-orange-400",
  processing: "bg-blue-500/20 text-blue-400",
  done:       "bg-green-500/20 text-green-400",
  failed:     "bg-red-500/20 text-red-400",
  cancelled:  "bg-gray-500/20 text-gray-400",
};

export default function HomePage() {
  const { token, logout, loading, authEnabled } = useAuth();
  const router = useRouter();
  const [videos, setVideos] = useState<Video[]>([]);
  const [categories, setCategories] = useState<string[]>([]);
  const [selectedCategory, setSelectedCategory] = useState<string>("all");
  const [url, setUrl] = useState("");
  const [category, setCategory] = useState("");
  const [error, setError] = useState("");
  const [hasMore, setHasMore] = useState(true);
  const [deleteId, setDeleteId] = useState<number | null>(null);
  const [showDialog, setShowDialog] = useState(false);
  const loaderRef = useRef<HTMLDivElement | null>(null);
  const [fetching, setFetching] = useState(false);
  const [downloads, setDownloads] = useState<DownloadJob[]>([]);
  const downloadsRef = useRef<DownloadJob[]>([]);

  // Keep ref in sync so polling interval always sees latest state
  useEffect(() => {
    downloadsRef.current = downloads;
  }, [downloads]);

  async function fetchCategories() {
    try {
      const cats = await listCategories(token!);
      setCategories(["all", ...cats.filter((c: string) => c !== "all")]);
    } catch {
      setError("Failed to load categories");
    }
  }

  const fetchMoreVideos = useCallback(
    async (reset = false) => {
      if (!token) return;
      setFetching(true);
      try {
        const skip = reset ? 0 : videos.length;
        const newVideos = await listVideos(token, selectedCategory, skip, PAGE_SIZE);
        setVideos((prev) => (reset ? newVideos : [...prev, ...newVideos]));
        setHasMore(newVideos.length === PAGE_SIZE);
      } catch {
        setError("Failed to load videos");
      } finally {
        setFetching(false);
      }
    },
    [token, selectedCategory, videos.length]
  );

  useEffect(() => {
    if (!loading && !token) router.replace("/login");
  }, [token, loading, router]);

  // Poll all active queue jobs in one interval
  const hasActiveQueueJobs = downloads.some(
    (d) => d.jobId && (d.status === "queued" || d.status === "processing")
  );

  // Treat "cancelled" returned by polling as a terminal state
  const TERMINAL = new Set(["done", "failed", "cancelled"]);

  useEffect(() => {
    if (!token || !hasActiveQueueJobs) return;

    const interval = setInterval(async () => {
      const active = downloadsRef.current.filter(
        (d) => d.jobId && (d.status === "queued" || d.status === "processing")
      );
      if (active.length === 0) return;

      await Promise.all(
        active.map(async (job) => {
          try {
            const data = await getQueueStatus(token, job.jobId!);

            if (TERMINAL.has(data.status)) {
              const isDone = data.status === "done";
              setDownloads((prev) =>
                prev.map((d) =>
                  d.localId === job.localId
                    ? {
                        ...d,
                        status: data.status,
                        message:
                          isDone ? "Video ready!" :
                          data.status === "cancelled" ? "Cancelled" :
                          `Failed: ${data.error}`,
                      }
                    : d
                )
              );
              if (isDone) {
                setVideos([]);
                setHasMore(true);
                fetchMoreVideos(true);
                setTimeout(() => {
                  setDownloads((prev) => prev.filter((d) => d.localId !== job.localId));
                }, 5000);
              }
            } else {
              const msg =
                data.status === "processing"
                  ? "Downloading and processing video..."
                  : "Queued for processing";
              setDownloads((prev) =>
                prev.map((d) =>
                  d.localId === job.localId
                    ? { ...d, status: data.status, message: msg, queuePosition: data.queue_position }
                    : d
                )
              );
            }
          } catch (err) {
            console.error("Polling error for job", job.jobId, err);
          }
        })
      );
    }, 3000);

    return () => clearInterval(interval);
  }, [token, hasActiveQueueJobs, fetchMoreVideos]);

  useEffect(() => {
    if (!token) return;
    setVideos([]);
    setHasMore(true);
    fetchCategories();
    fetchMoreVideos(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, selectedCategory]);

  // Infinite scroll
  useEffect(() => {
    if (!hasMore || loading) return;
    const observer = new window.IntersectionObserver(
      (entries) => {
        if (entries[0].isIntersecting && !fetching) fetchMoreVideos();
      },
      { threshold: 1 }
    );
    if (loaderRef.current) observer.observe(loaderRef.current);
    return () => {
      if (loaderRef.current) observer.unobserve(loaderRef.current);
    };
    // eslint-disable-next-line
  }, [loaderRef.current, hasMore, loading, fetching]);

  async function handleAddVideo(e: { preventDefault(): void }) {
    e.preventDefault();
    if (!url || !category) return;

    const localId = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const submittedUrl = url;
    const submittedCategory = category;
    const controller = new AbortController();

    // Clear form immediately so the user can queue the next video right away
    setUrl("");
    setCategory("");
    setError("");

    setDownloads((prev) => [
      ...prev,
      { localId, url: submittedUrl, status: "extracting", message: "Extracting video metadata...", abortController: controller },
    ]);

    try {
      let realUrl = submittedUrl;
      let title: string | undefined;
      let duration: number | undefined;

      try {
        const res = await extractVideo(token!, submittedUrl, controller.signal);
        if (res.video_url) realUrl = res.video_url;
        if (res.title) title = res.title;
        if (res.duration) duration = res.duration;

        if (title) {
          setDownloads((prev) =>
            prev.map((d) => (d.localId === localId ? { ...d, title } : d))
          );
        }

        if (res.job_id) {
          setDownloads((prev) =>
            prev.map((d) =>
              d.localId === localId
                ? {
                    ...d,
                    status: "queued",
                    message: res.message || "Video queued for processing.",
                    jobId: res.job_id,
                  }
                : d
            )
          );
          return;
        }
      } catch {}

      setDownloads((prev) =>
        prev.map((d) =>
          d.localId === localId ? { ...d, status: "adding", message: "Saving video..." } : d
        )
      );

      try {
        await addVideo(token!, realUrl, submittedCategory, title, duration);
      } catch (addErr: unknown) {
        const axiosErr = addErr as { response?: { status?: number } };
        if (axiosErr?.response?.status !== 409) throw addErr;
      }

      setDownloads((prev) =>
        prev.map((d) =>
          d.localId === localId
            ? { ...d, title: title || d.url, status: "done", message: "Video added!" }
            : d
        )
      );
      setVideos([]);
      setHasMore(true);
      fetchMoreVideos(true);

      setTimeout(() => {
        setDownloads((prev) => prev.filter((d) => d.localId !== localId));
      }, 4000);
    } catch (err: unknown) {
      // Silently drop aborted requests (user clicked cancel)
      const isAbort = (err as { name?: string; code?: string })?.name === "CanceledError"
        || (err as { code?: string })?.code === "ERR_CANCELED";
      if (isAbort) return;
      setDownloads((prev) =>
        prev.map((d) =>
          d.localId === localId
            ? { ...d, status: "failed", message: "Failed to add video" }
            : d
        )
      );
    }
  }

  function handlePaste(e: React.ClipboardEvent<HTMLInputElement>) {
    setUrl(e.clipboardData.getData("text").trim());
    e.preventDefault();
  }

  async function handleDelete(id: number) {
    setDeleteId(id);
    setShowDialog(true);
  }

  async function confirmDelete() {
    if (deleteId == null) return;
    try {
      await deleteVideo(token!, deleteId);
      setVideos([]);
      setHasMore(true);
      fetchMoreVideos(true);
      setDeleteId(null);
      setShowDialog(false);
    } catch {
      setError("Failed to delete video");
    }
  }

  function formatDuration(seconds?: number) {
    if (!seconds || isNaN(seconds)) return "Unknown";
    const m = Math.floor(seconds / 60);
    const s = Math.round(seconds % 60);
    return `${m}:${s.toString().padStart(2, "0")}`;
  }

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center">Loading...</div>
    );
  }

  const activeCount = downloads.filter(
    (d) => d.status !== "done" && d.status !== "failed"
  ).length;

  return (
    <div className="min-h-screen text-white pb-20">
      {/* Header */}
      <div className="flex justify-between items-center px-8 py-5 glass-panel sticky top-0 z-50 rounded-b-2xl mx-4 mb-10 shadow-xl shadow-indigo-500/10">
        <h1
          className="text-2xl font-bold bg-clip-text text-transparent bg-linear-to-r from-indigo-400 to-purple-400 cursor-pointer"
          onClick={() => window.location.reload()}
        >
          VidQ
        </h1>
        {authEnabled && (
          <Button
            variant="outline"
            onClick={logout}
            className="border-white/10 bg-transparent hover:bg-white/10 hover:text-white transition-all rounded-xl text-gray-200"
          >
            Logout
          </Button>
        )}
      </div>

      <div className="max-w-6xl mx-auto px-4 sm:px-6">
        {/* Add Video Form */}
        <div className="glass-panel p-6 md:p-8 rounded-4xl mb-8 shadow-2xl shadow-purple-500/5 transform transition-all hover:scale-[1.01] duration-500">
          <form onSubmit={handleAddVideo} className="flex flex-col sm:flex-row gap-4">
            <Input
              placeholder="Video URL (YouTube, Vimeo, etc)"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              onPaste={handlePaste}
              className="flex-1 bg-white/5 border-white/10 focus-visible:ring-indigo-500 rounded-xl h-14 text-white placeholder:text-gray-400 px-5 text-base"
            />
            <Input
              placeholder="Category Name"
              value={category}
              onChange={(e) => setCategory(e.target.value)}
              className="w-full sm:w-56 bg-white/5 border-white/10 focus-visible:ring-indigo-500 rounded-xl h-14 text-white placeholder:text-gray-400 px-5 text-base"
            />
            <Button
              type="submit"
              disabled={!url || !category}
              className="sm:w-auto w-full h-14 px-8 font-semibold rounded-xl bg-linear-to-r from-indigo-500 to-purple-600 hover:from-indigo-400 hover:to-purple-500 transition-all border-none shadow-lg shadow-indigo-500/25 text-white disabled:opacity-50"
            >
              Add Video
            </Button>
          </form>
        </div>

        {error && <div className="text-red-500 mb-4">{error}</div>}

        {/* Downloads Dashboard */}
        {downloads.length > 0 && (
          <div className="mb-10">
            <div className="flex items-center gap-3 mb-4">
              <h2 className="text-base font-semibold text-white">Downloads</h2>
              {activeCount > 0 && (
                <span className="flex items-center gap-1.5 text-xs text-indigo-300 bg-indigo-500/10 border border-indigo-500/20 px-2.5 py-1 rounded-full">
                  <span className="w-1.5 h-1.5 rounded-full bg-indigo-400 animate-pulse" />
                  {activeCount} active
                </span>
              )}
            </div>
            <div className="space-y-2.5">
              {downloads.map((dl) => (
                <div
                  key={dl.localId}
                  className={`glass-panel px-5 py-4 rounded-2xl border flex items-center gap-4 transition-all ${
                    dl.status === "done"
                      ? "border-green-500/25"
                      : dl.status === "failed"
                      ? "border-red-500/25"
                      : "border-indigo-500/20"
                  }`}
                >
                  {/* Status icon */}
                  <div className="shrink-0">
                    {dl.status === "done" ? (
                      <div className="w-8 h-8 rounded-full bg-green-500/15 flex items-center justify-center">
                        <Check className="w-4 h-4 text-green-400" />
                      </div>
                    ) : dl.status === "failed" ? (
                      <div className="w-8 h-8 rounded-full bg-red-500/15 flex items-center justify-center">
                        <X className="w-4 h-4 text-red-400" />
                      </div>
                    ) : (
                      <div className="w-8 h-8 rounded-full bg-indigo-500/15 flex items-center justify-center">
                        <Loader2 className="w-4 h-4 text-indigo-400 animate-spin" />
                      </div>
                    )}
                  </div>

                  {/* Info */}
                  <div className="flex-1 min-w-0">
                    <p className="text-sm font-medium text-white truncate">
                      {dl.title || dl.url}
                    </p>
                    <p
                      className={`text-xs mt-0.5 ${
                        dl.status === "done"
                          ? "text-green-400"
                          : dl.status === "failed"
                          ? "text-red-400"
                          : "text-indigo-400"
                      }`}
                    >
                      {dl.message}
                      {dl.queuePosition && dl.status === "queued"
                        ? ` · Position: ${dl.queuePosition}`
                        : ""}
                    </p>
                    {dl.jobId && (
                      <p className="text-[10px] text-gray-600 font-mono mt-0.5">
                        Job: {dl.jobId.slice(0, 16)}…
                      </p>
                    )}
                  </div>

                  {/* Status badge */}
                  <span
                    className={`hidden sm:inline text-[10px] px-2.5 py-1 rounded-full font-medium uppercase tracking-wider shrink-0 ${STATUS_STYLES[dl.status]}`}
                  >
                    {dl.status}
                  </span>

                  {/* Cancel — extracting/adding: abort HTTP request */}
                  {(dl.status === "extracting" || dl.status === "adding") && (
                    <button
                      title="Cancel"
                      onClick={() => {
                        dl.abortController?.abort();
                        setDownloads((prev) => prev.filter((d) => d.localId !== dl.localId));
                      }}
                      className="shrink-0 text-orange-400 hover:text-white transition-colors ml-1"
                    >
                      <X className="w-4 h-4" />
                    </button>
                  )}

                  {/* Cancel — queued/processing: call cancel API */}
                  {dl.jobId && (dl.status === "queued" || dl.status === "processing") && (
                    <button
                      title="Cancel"
                      onClick={async () => {
                        try {
                          await cancelJob(token!, dl.jobId!);
                          setDownloads((prev) =>
                            prev.map((d) =>
                              d.localId === dl.localId
                                ? { ...d, status: "cancelled", message: "Cancelled by user" }
                                : d
                            )
                          );
                        } catch {
                          setDownloads((prev) => prev.filter((d) => d.localId !== dl.localId));
                        }
                      }}
                      className="shrink-0 text-orange-400 hover:text-white transition-colors ml-1"
                    >
                      <X className="w-4 h-4" />
                    </button>
                  )}

                  {/* Dismiss — terminal states */}
                  {(dl.status === "done" || dl.status === "failed" || dl.status === "cancelled") && (
                    <button
                      title="Dismiss"
                      onClick={() =>
                        setDownloads((prev) =>
                          prev.filter((d) => d.localId !== dl.localId)
                        )
                      }
                      className="shrink-0 text-gray-500 hover:text-white transition-colors ml-1"
                    >
                      <X className="w-4 h-4" />
                    </button>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Category Tabs */}
        <div className="flex justify-center mb-12">
          <Tabs
            value={selectedCategory}
            onValueChange={setSelectedCategory}
            className="w-full max-w-3xl"
          >
            <TabsList className="bg-white/5 border border-white/10 p-1.5 rounded-2xl w-full flex overflow-x-auto hide-scrollbar h-auto">
              {categories.map((cat) => (
                <TabsTrigger
                  key={cat}
                  value={cat}
                  className="capitalize rounded-xl px-6 py-3 text-sm font-medium transition-all data-[state=active]:bg-indigo-500 data-[state=active]:text-white data-[state=active]:shadow-lg data-[state=active]:shadow-indigo-500/30 text-gray-400 hover:text-white"
                >
                  {cat || "all"}
                </TabsTrigger>
              ))}
            </TabsList>
          </Tabs>
        </div>

        {/* Video Grid */}
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-8">
          {videos.length === 0 && !loading && (
            <div className="text-gray-500 col-span-3">No videos found.</div>
          )}
          {videos.map((video) => (
            <Card
              key={`${video.id}-${video.url}`}
              className="glass-panel overflow-hidden flex flex-col min-h-90 min-w-[320px] rounded-3xl border border-white/10 hover:border-indigo-500/30 shadow-xl hover:shadow-indigo-500/20 transform transition-all hover:-translate-y-2 duration-500 group bg-transparent"
            >
              {video.thumbnail && (
                <div className="w-full aspect-video relative overflow-hidden bg-black/40">
                  <Image
                    src={video.thumbnail}
                    alt={video.title || "Video thumbnail"}
                    fill
                    className="object-cover group-hover:scale-105 transition-transform duration-700 ease-out opacity-90 group-hover:opacity-100"
                    sizes="(max-width: 768px) 100vw, (max-width: 1200px) 50vw, 33vw"
                    priority={true}
                  />
                  <div className="absolute inset-0 bg-linear-to-t from-black/80 via-transparent to-transparent" />
                </div>
              )}
              <CardHeader className="pb-3 pt-5 relative z-10">
                <CardTitle
                  className="text-lg font-bold wrap-break-word whitespace-pre-line leading-tight truncate text-white drop-shadow-md"
                  title={video.title}
                >
                  {video.title || "Untitled Video"}
                </CardTitle>
              </CardHeader>
              <CardContent className="flex-1 flex flex-col justify-between z-10 px-6">
                <div className="mb-5 text-xs flex gap-2">
                  <span className="inline-block px-3 py-1.5 bg-white/10 rounded-full text-indigo-300 font-medium tracking-wider uppercase text-[10px] border border-white/5">
                    {video.category}
                  </span>
                  {video.duration !== undefined && video.duration !== null && (
                    <span className="inline-block px-3 py-1.5 bg-black/30 rounded-full text-gray-300 font-mono text-[10px] border border-white/5">
                      {formatDuration(video.duration)}
                    </span>
                  )}
                </div>
                <div className="flex justify-between items-center rounded-xl overflow-hidden shadow-inner bg-black/40 relative group-hover:shadow-indigo-500/20 transition-all aspect-video">
                  <video
                    className="w-full h-full object-cover opacity-70 group-hover:opacity-100 transition-opacity duration-300"
                    controls
                    preload="metadata"
                  >
                    <source src={video.url} type="video/mp4" />
                    <source src={video.url} type="video/webm" />
                    Your browser does not support the video tag.
                  </video>
                </div>
              </CardContent>
              <CardFooter className="flex justify-end gap-2 pt-0 pb-5 pr-6 z-10">
                <Button
                  variant="outline"
                  size="icon"
                  title="Download Video"
                  onClick={async (e) => {
                    e.stopPropagation();
                    try {
                      const blob = await downloadVideo(token!, video.id);
                      const blobUrl = window.URL.createObjectURL(blob);
                      const a = document.createElement("a");
                      a.href = blobUrl;
                      const ext = video.url.split(".").pop()?.split("?")[0] || "mp4";
                      a.download = video.title
                        ? `${video.title.replace(/[^a-z0-9]/gi, "_").toLowerCase()}.${ext}`
                        : `video-${video.id}.${ext}`;
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
                    handleDelete(video.id);
                  }}
                  className="h-10 w-10 p-0 rounded-full bg-red-500/10 text-red-400 hover:bg-red-500 hover:text-white border border-red-500/20 transition-all shadow hover:shadow-red-500/40"
                >
                  <Trash className="w-4 h-4" />
                </Button>
              </CardFooter>
            </Card>
          ))}
        </div>
        <div ref={loaderRef} className="h-8" />

        {/* Delete Confirmation */}
        <Dialog open={showDialog} onOpenChange={setShowDialog}>
          <DialogContent className="glass-panel border-white/10 bg-gray-950/90 text-white rounded-4xl p-6 sm:p-8 shadow-2xl shadow-black">
            <DialogHeader>
              <DialogTitle className="text-xl">Delete Video</DialogTitle>
            </DialogHeader>
            <div className="text-gray-300 my-2">
              Are you sure you want to delete this video? This action cannot be undone.
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
    </div>
  );
}
