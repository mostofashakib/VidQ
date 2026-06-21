import argparse
import os
from pathlib import Path

import cv2
import torch
from basicsr.archs.rrdbnet_arch import RRDBNet
from realesrgan import RealESRGANer


def _select_device() -> torch.device | None:
    if torch.backends.mps.is_available():
        return torch.device("mps")
    if torch.cuda.is_available():
        return torch.device("cuda")
    return None


def _build_upsampler(model_path: str, tile: int) -> RealESRGANer:
    model = RRDBNet(
        num_in_ch=3,
        num_out_ch=3,
        num_feat=64,
        num_block=23,
        num_grow_ch=32,
        scale=4,
    )
    device = _select_device()
    return RealESRGANer(
        scale=4,
        model_path=model_path,
        dni_weight=None,
        model=model,
        tile=tile,
        tile_pad=10,
        pre_pad=0,
        half=False,
        device=device,
    )


def _iter_frames(input_dir: str) -> list[Path]:
    supported = {".jpg", ".jpeg", ".png", ".webp"}
    return sorted(
        path for path in Path(input_dir).iterdir()
        if path.is_file() and path.suffix.lower() in supported
    )


def enhance_frames(input_dir: str, output_dir: str, model_path: str, tile: int) -> None:
    frames = _iter_frames(input_dir)
    if not frames:
        raise RuntimeError(f"No supported frames found in {input_dir}")

    os.makedirs(output_dir, exist_ok=True)
    upsampler = _build_upsampler(model_path, tile)

    for frame_path in frames:
        image = cv2.imread(str(frame_path), cv2.IMREAD_UNCHANGED)
        if image is None:
            raise RuntimeError(f"Could not read frame: {frame_path}")
        output, _ = upsampler.enhance(image, outscale=4)
        output_path = Path(output_dir) / f"{frame_path.stem}.png"
        if not cv2.imwrite(str(output_path), output):
            raise RuntimeError(f"Could not write enhanced frame: {output_path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--tile", type=int, default=0)
    args = parser.parse_args()
    enhance_frames(args.input, args.output, args.model, args.tile)


if __name__ == "__main__":
    main()
