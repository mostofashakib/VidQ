#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="${ROOT_DIR}/backend"
FRONTEND_DIR="${ROOT_DIR}/frontend"

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
export UV_CACHE_DIR="${UV_CACHE_DIR:-${ROOT_DIR}/.uv-cache}"

log() {
    echo "==> $1"
}

ensure_command() {
    local name="$1"
    local install_hint="$2"
    if ! command -v "$name" >/dev/null 2>&1; then
        echo "Missing required command: $name" >&2
        echo "$install_hint" >&2
        exit 1
    fi
}

update_env_value() {
    local key="$1"
    local value="$2"
    local env_file="${BACKEND_DIR}/.env"

    if [ ! -f "$env_file" ]; then
        return
    fi

    if grep -q "^${key}=" "$env_file"; then
        sed -i.bak "s|^${key}=.*|${key}=${value}|" "$env_file"
        rm -f "${env_file}.bak"
    else
        printf '\n%s=%s\n' "$key" "$value" >> "$env_file"
    fi
}

install_real_esrgan_macos() {
    local version="v0.2.5.0"
    local archive_name="realesrgan-ncnn-vulkan-20220424-macos.zip"
    local download_url="https://github.com/xinntao/Real-ESRGAN/releases/download/${version}/${archive_name}"
    local install_root="${REAL_ESRGAN_INSTALL_ROOT:-$HOME/.local/opt/realesrgan-ncnn-vulkan}"
    local bin_dir="${HOME}/.local/bin"
    local tmp_dir
    local extracted_bin

    tmp_dir="$(mktemp -d)"
    cleanup_real_esrgan_tmp() {
        rm -rf "$tmp_dir"
    }
    trap cleanup_real_esrgan_tmp RETURN

    ensure_command curl "Install curl, then rerun ./setup.sh."
    ensure_command unzip "Install unzip, then rerun ./setup.sh."

    log "Downloading Real-ESRGAN ncnn Vulkan ${version}"
    curl -L --fail "$download_url" -o "${tmp_dir}/${archive_name}"

    log "Installing Real-ESRGAN into ${install_root}"
    rm -rf "$install_root"
    mkdir -p "$install_root" "$bin_dir"
    unzip -q "${tmp_dir}/${archive_name}" -d "$tmp_dir/extracted"

    extracted_bin="$(find "$tmp_dir/extracted" -type f -name realesrgan-ncnn-vulkan -print | sed -n '1p')"
    if [ -z "$extracted_bin" ]; then
        echo "Downloaded archive did not contain realesrgan-ncnn-vulkan." >&2
        exit 1
    fi

    cp -R "$(dirname "$extracted_bin")/." "$install_root/"
    chmod +x "${install_root}/realesrgan-ncnn-vulkan"
    ln -sf "${install_root}/realesrgan-ncnn-vulkan" "${bin_dir}/realesrgan-ncnn-vulkan"
    update_env_value "REAL_ESRGAN_BIN" "${install_root}/realesrgan-ncnn-vulkan"

    echo "Installed: ${install_root}/realesrgan-ncnn-vulkan"
    echo "Symlinked: ${bin_dir}/realesrgan-ncnn-vulkan"
}

install_python_realesrgan() {
    local venv_dir="${BACKEND_DIR}/.realesrgan-venv"
    local model_dir="${BACKEND_DIR}/models/realesrgan"
    local model_path="${model_dir}/RealESRGAN_x4plus.pth"
    local model_url="https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth"
    local python_bin="${venv_dir}/bin/python"

    ensure_command curl "Install curl, then rerun ./setup.sh."

    if [ ! -x "$python_bin" ]; then
        log "Creating Python Real-ESRGAN backend venv"
        if command -v uv >/dev/null 2>&1; then
            uv python install 3.11
            uv venv --python 3.11 "$venv_dir"
        else
            python3 -m venv "$venv_dir"
        fi
    else
        echo "Found Python Real-ESRGAN backend venv: ${python_bin}"
    fi

    if ! "$python_bin" - <<'PY' >/dev/null 2>&1
import cv2
import torch
import basicsr
import facexlib
import gfpgan
import realesrgan
PY
    then
        log "Installing Python Real-ESRGAN backend packages"
        if ! "$python_bin" -m pip --version >/dev/null 2>&1; then
            "$python_bin" -m ensurepip --upgrade || true
        fi
        if "$python_bin" -m pip --version >/dev/null 2>&1; then
            "$python_bin" -m pip install --upgrade pip
            "$python_bin" -m pip install "numpy<2" "torch==2.1.2" "torchvision==0.16.2"
            "$python_bin" -m pip install "opencv-python==4.9.0.80" basicsr facexlib gfpgan realesrgan
        elif command -v uv >/dev/null 2>&1; then
            uv pip install --python "$python_bin" "numpy<2" "torch==2.1.2" "torchvision==0.16.2"
            uv pip install --python "$python_bin" "opencv-python==4.9.0.80" basicsr facexlib gfpgan realesrgan
        else
            echo "Could not install Python Real-ESRGAN packages because pip is missing." >&2
            echo "Install pip in ${venv_dir}, or install uv and rerun ./setup.sh." >&2
            exit 1
        fi
    else
        echo "Python Real-ESRGAN packages already installed"
    fi

    mkdir -p "$model_dir"
    if [ ! -f "$model_path" ]; then
        log "Downloading RealESRGAN_x4plus weights"
        curl -L --fail "$model_url" -o "$model_path"
    fi

    update_env_value "REAL_ESRGAN_BACKEND" "auto"
    update_env_value "REAL_ESRGAN_PYTHON" "$python_bin"
    update_env_value "REAL_ESRGAN_MODEL_PATH" "$model_path"

    echo "Installed Python Real-ESRGAN backend: ${python_bin}"
    echo "Installed model weights: ${model_path}"
}

log "Checking required runtimes"
ensure_command python3 "Install Python 3.10+ from https://www.python.org/downloads/."

if ! command -v node >/dev/null 2>&1; then
    if command -v brew >/dev/null 2>&1; then
        log "Installing Node.js with Homebrew"
        brew install node
    else
        echo "Missing required command: node" >&2
        echo "Install Node.js 18+ from https://nodejs.org/." >&2
        exit 1
    fi
fi

ensure_command npm "Install npm with Node.js 18+ from https://nodejs.org/."

if ! command -v uv >/dev/null 2>&1; then
    log "Installing uv"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

log "Creating backend/.env if needed"
if [ ! -f "${BACKEND_DIR}/.env" ]; then
    cp "${BACKEND_DIR}/.env.example" "${BACKEND_DIR}/.env"
    echo "Created backend/.env from backend/.env.example"
fi

log "Installing backend dependencies"
cd "$BACKEND_DIR"
if [ ! -d ".venv" ]; then
    uv venv
fi
uv pip install -r requirements.txt
uv pip install -r requirements-dev.txt

if [ "${SKIP_PLAYWRIGHT:-0}" = "1" ]; then
    echo "Skipped Playwright browser install because SKIP_PLAYWRIGHT=1"
else
    log "Installing Playwright Chromium"
    uv run playwright install chromium
fi

log "Installing frontend dependencies"
cd "$FRONTEND_DIR"
if [ ! -d "node_modules" ] || [ "${FORCE_INSTALL:-0}" = "1" ]; then
    if [ -f "package-lock.json" ]; then
        npm ci
    else
        npm install
    fi
fi

log "Checking Enhance dependency"
if command -v realesrgan-ncnn-vulkan >/dev/null 2>&1; then
    real_esrgan_bin="$(command -v realesrgan-ncnn-vulkan)"
    echo "Found realesrgan-ncnn-vulkan: ${real_esrgan_bin}"
    update_env_value "REAL_ESRGAN_BIN" "$real_esrgan_bin"
elif [ "${SKIP_REAL_ESRGAN:-0}" = "1" ]; then
    echo "Skipped Real-ESRGAN install because SKIP_REAL_ESRGAN=1"
elif [ "$(uname -s)" = "Darwin" ]; then
    install_real_esrgan_macos
else
    echo "Real-ESRGAN is only auto-installed by this script on macOS."
    echo "Install realesrgan-ncnn-vulkan manually, then set REAL_ESRGAN_BIN in backend/.env."
fi

if [ "${SKIP_PYTHON_REALESRGAN:-0}" = "1" ]; then
    echo "Skipped Python Real-ESRGAN backend because SKIP_PYTHON_REALESRGAN=1"
else
    install_python_realesrgan
fi

log "Setup complete"
echo "Run ./run.sh and open http://localhost:3000"
