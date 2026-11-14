export interface Video {
  id: number;
  url: string;
  category: string;
  title?: string;
  created_at: string;
  duration?: number;
  thumbnail?: string;
}

export interface VideoDimensions {
  width: number;
  height: number;
}
