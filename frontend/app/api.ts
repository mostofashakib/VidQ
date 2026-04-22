import axios from "axios";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export async function login(password: string) {
  const res = await axios.post(`${API_URL}/auth/login`, { password });
  return res.data.token as string;
}

export async function addVideo(token: string, url: string, category: string, title?: string, duration?: number) {
  const res = await axios.post(
    `${API_URL}/videos`,
    { url, category, title, duration },
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

export async function listCategories(token: string) {
  const res = await axios.get(`${API_URL}/videos/categories`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  return res.data;
}

export async function deleteVideo(token: string, id: number) {
  await axios.delete(`${API_URL}/videos/${id}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
}

export async function extractVideo(token: string, url: string, signal?: AbortSignal) {
  const res = await axios.post(
    `${API_URL}/extract-video`,
    { url },
    { headers: { Authorization: `Bearer ${token}` }, signal }
  );
  return res.data;
}

export async function enqueueVideo(token: string, url: string, category: string) {
  const res = await axios.post(
    `${API_URL}/queue`,
    { url, category },
    { headers: { Authorization: `Bearer ${token}` } }
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