type ResultVideoPlayerProps = {
  src: string;
  title: string;
};

export default function ResultVideoPlayer({ src, title }: ResultVideoPlayerProps) {
  return (
    <div className="mt-3 rounded-xl overflow-hidden shadow-inner bg-black/40 aspect-video">
      <video
        key={src}
        controls
        className="w-full h-full object-contain bg-black"
        title={title}
      >
        <source src={src} type="video/mp4" />
        <source src={src} type="video/webm" />
        Your browser does not support the video tag.
      </video>
    </div>
  );
}
