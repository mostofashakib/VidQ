import axios from "axios";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export async function login(password: string) {
  const res = await axios.post(`${API_URL}/auth/login`, { password });
  return res.data.token as string;
}

export async function addVideo(token: string, url: string, title?: string, duration?: number) {
  const res = await axios.post(
    `${API_URL}/videos`,
    { url, title, duration },
    { headers: { Authorization: `Bearer ${token}` } }
  );
  return res.data;
}

export async function listVideos(token: string, category?: string, skip?: number, limit?: number) {
  const params: Record<string, string | number> = category && category !== "all" ? { category } : {};
  if (typeof skip === "number") params.skip = skip;
  if (typeof limit === "number") params.limit = limit;
  const res = await axios.get(`${API_URL}/videos`, {
    headers: { Authorization: `Bearer ${token}` },
    params,
  });
  return res.data;
}

export async function deleteVideo(token: string, id: number) {
  await axios.delete(`${API_URL}/videos/${id}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
}

// ── Search ────────────────────────────────────────────────────────────────

export interface SearchResultItem {
  url: string;
  title: string;
  source: string;
  duration: number | null;
  thumbnail: string;
  snippet: string;
  reason: string;
}

export interface SearchData {
  search_id: string;
  description: string;
  status: "running" | "done" | "failed";
  phase: "planning" | "searching" | "ranking" | "comparing" | null;
  queries: string[];
  results: SearchResultItem[];
  has_more: boolean;
  ranked_by_llm: boolean;
  error: string | null;
}

export async function startSearch(token: string, description: string): Promise<SearchData> {
  const res = await axios.post(`${API_URL}/search`, { description }, {
    headers: { Authorization: `Bearer ${token}` },
  });
  return res.data;
}

export async function getSearch(token: string, searchId: string): Promise<SearchData> {
  const res = await axios.get(`${API_URL}/search/${searchId}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  return res.data;
}

export async function moreSearchResults(token: string, searchId: string): Promise<SearchData> {
  const res = await axios.post(`${API_URL}/search/${searchId}/more`, {}, {
    headers: { Authorization: `Bearer ${token}` },
  });
  return res.data;
}

/** How to play a search result: relayed through the backend, or in the site's own player. */
export type PlayData =
  | { mode: "stream"; stream_id: string; kind: "file" | "hls"; path: string }
  | { mode: "embed"; embed_url: string };

export async function playSearchResult(token: string, url: string): Promise<PlayData> {
  const res = await axios.post(`${API_URL}/search/play`, { url }, {
    headers: { Authorization: `Bearer ${token}` },
  });
  return res.data;
}

/** Absolute URL of a relayed stream path. Stream IDs are the access key, so no token is sent. */
export function streamUrl(path: string): string {
  return `${API_URL}${path}`;
}

/** One queued job. Album links return one per video. */
export interface QueuedJob {
  job_id: string;
  title?: string | null;
}

export async function extractVideo(token: string, url: string, signal?: AbortSignal) {
  const res = await axios.post(
    `${API_URL}/extract-video`,
    { url },
    { headers: { Authorization: `Bearer ${token}` }, signal }
  );
  return res.data;
}

export async function getQueueStatus(token: string, jobId: string) {
  const res = await axios.get(`${API_URL}/queue/${jobId}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  return res.data;
}

export async function getAuthStatus(): Promise<{ auth_enabled: boolean }> {
  const res = await axios.get(`${API_URL}/auth/status`);
  return res.data;
}

export async function cancelJob(token: string, jobId: string) {
  const res = await axios.delete(`${API_URL}/queue/${jobId}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  return res.data;
}

export async function downloadVideo(token: string, id: number): Promise<Blob> {
  const res = await axios.get(`${API_URL}/videos/${id}/download`, {
    headers: { Authorization: `Bearer ${token}` },
    responseType: "blob",
  });
  return res.data;
}

export interface UploadJobData {
  job_id: string;
  filename: string;
  status: string;
  video_id?: number;
  error?: string;
  scale_progress?: number;
}

export async function uploadVideo(
  token: string,
  file: File,
  onProgress?: (percent: number) => void,
  signal?: AbortSignal
): Promise<UploadJobData> {
  const form = new FormData();
  form.append("file", file);

  const res = await axios.post(`${API_URL}/upload-video`, form, {
    headers: { Authorization: `Bearer ${token}` },
    signal,
    onUploadProgress: (e) => {
      if (onProgress && e.total) {
        onProgress(Math.round((e.loaded / e.total) * 100));
      }
    },
  });
  return res.data;
}

export async function getUploadJob(token: string, jobId: string): Promise<UploadJobData> {
  const res = await axios.get(`${API_URL}/upload-jobs/${jobId}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  return res.data;
}

export async function cancelUploadJob(token: string, jobId: string): Promise<void> {
  await axios.delete(`${API_URL}/upload-jobs/${jobId}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
}

export async function deleteAllVideos(token: string): Promise<void> {
  await axios.delete(`${API_URL}/videos`, {
    headers: { Authorization: `Bearer ${token}` },
  });
}

export async function deleteAllUploadVideos(token: string): Promise<void> {
  await axios.delete(`${API_URL}/upload-videos`, {
    headers: { Authorization: `Bearer ${token}` },
  });
}

export async function listUploadedVideos(token: string): Promise<
  Array<{
    id: number; url: string; category: string; title?: string;
    duration?: number; thumbnail?: string; source: string; created_at: string;
  }>
> {
  const res = await axios.get(`${API_URL}/upload-videos`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  return res.data;
}

// ── Combine ────────────────────────────────────────────────────────────────

export interface CombineJobData {
  job_id: string;
  status: string;
  phase: string;
  overall_progress: number;
  clip_index: number;
  total_clips: number;
  result_url?: string;
  error?: string;
}

export async function startCombineJob(
  token: string,
  files: File[],
  onUploadProgress?: (percent: number) => void,
  signal?: AbortSignal,
): Promise<CombineJobData> {
  const form = new FormData();
  files.forEach((f) => form.append("files", f));
  const res = await axios.post(`${API_URL}/combine-video`, form, {
    headers: { Authorization: `Bearer ${token}` },
    signal,
    onUploadProgress: (e) => {
      if (onUploadProgress && e.total) {
        onUploadProgress(Math.round((e.loaded / e.total) * 100));
      }
    },
  });
  return res.data;
}

export async function getCombineJob(token: string, jobId: string): Promise<CombineJobData> {
  const res = await axios.get(`${API_URL}/combine-jobs/${jobId}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  return res.data;
}

export async function cancelCombineJob(token: string, jobId: string): Promise<void> {
  await axios.delete(`${API_URL}/combine-jobs/${jobId}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
}

// ── Translate ──────────────────────────────────────────────────────────────

export interface TranslateJobData {
  job_id: string;
  filename: string;
  status: string;
  phase: string;
  overall_progress: number;
  chunk_index: number;
  total_chunks: number;
  result_url?: string;
  error?: string;
}

export async function startTranslateJob(
  token: string,
  file: File,
  onUploadProgress?: (percent: number) => void,
  signal?: AbortSignal,
): Promise<TranslateJobData> {
  const form = new FormData();
  form.append("file", file);
  const res = await axios.post(`${API_URL}/translate-video`, form, {
    headers: { Authorization: `Bearer ${token}` },
    signal,
    onUploadProgress: (e) => {
      if (onUploadProgress && e.total) {
        onUploadProgress(Math.round((e.loaded / e.total) * 100));
      }
    },
  });
  return res.data;
}

export async function getTranslateJob(token: string, jobId: string): Promise<TranslateJobData> {
  const res = await axios.get(`${API_URL}/translate-jobs/${jobId}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  return res.data;
}

export async function cancelTranslateJob(token: string, jobId: string): Promise<void> {
  await axios.delete(`${API_URL}/translate-jobs/${jobId}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
}

// ── Trim ───────────────────────────────────────────────────────────────────

export interface TrimJobData {
  job_id: string;
  status: string;
  progress: number;
  result_url?: string;
  error?: string;
}

export async function startTrimJob(
  token: string,
  file: File,
  startTime: number,
  endTime: number,
  onUploadProgress?: (percent: number) => void,
): Promise<TrimJobData> {
  const form = new FormData();
  form.append("file", file);
  form.append("start_time", String(startTime));
  form.append("end_time", String(endTime));
  const res = await axios.post(`${API_URL}/trim-video`, form, {
    headers: { Authorization: `Bearer ${token}` },
    onUploadProgress: (e) => {
      if (onUploadProgress && e.total) {
        onUploadProgress(Math.round((e.loaded / e.total) * 100));
      }
    },
  });
  return res.data;
}

export async function getTrimJob(token: string, jobId: string): Promise<TrimJobData> {
  const res = await axios.get(`${API_URL}/trim-jobs/${jobId}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  return res.data;
}

export async function cancelTrimJob(token: string, jobId: string): Promise<void> {
  await axios.delete(`${API_URL}/trim-jobs/${jobId}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
}

// ── Enhance ────────────────────────────────────────────────────────────────

export interface EnhanceJobData {
  job_id: string;
  status: string;
  phase: string;
  progress: number;
  result_url?: string;
  error?: string;
}

export async function startEnhanceJob(
  token: string,
  file: File,
  onUploadProgress?: (percent: number) => void,
): Promise<EnhanceJobData> {
  const form = new FormData();
  form.append("file", file);
  const res = await axios.post(`${API_URL}/enhance-video`, form, {
    headers: { Authorization: `Bearer ${token}` },
    onUploadProgress: (e) => {
      if (onUploadProgress && e.total) {
        onUploadProgress(Math.round((e.loaded / e.total) * 100));
      }
    },
  });
  return res.data;
}

export async function getEnhanceJob(token: string, jobId: string): Promise<EnhanceJobData> {
  const res = await axios.get(`${API_URL}/enhance-jobs/${jobId}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  return res.data;
}

export async function cancelEnhanceJob(token: string, jobId: string): Promise<void> {
  await axios.delete(`${API_URL}/enhance-jobs/${jobId}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
}
