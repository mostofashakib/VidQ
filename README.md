# VidQ

An agentic video downloader that enables you to download multiple videos from anywhere on the web, in parallel. VidQ uses LLM-driven vision to autonomously navigate any video hosting site — bypassing overlays, interacting with custom players, and capturing high-quality content directly from the browser.

## Features

- **Parallel Downloads**: Queue multiple videos from different sources and download them simultaneously.
- **Agentic Navigation**: Vision LLMs (OpenAI/Anthropic) autonomously find and interact with video players on any site — no site-specific configuration needed.
- **Universal Compatibility**: Works with virtually any video hosting platform by capturing content directly from the browser via MediaRecorder.
- **Smart Recovery**: Automatically detects and dismisses ad overlays, age-gates, and cookie banners that block playback.
- **Quality Selection**: Attempts to set the highest available quality (720p+) before recording.
- **Modern UI**: Clean, responsive web interface for managing your download queue.

## Architecture

- **Backend**: FastAPI with Playwright (Chromium) and asynchronous workers.
- **Frontend**: Next.js 15 (App Router) with custom CSS.
- **AI**: Fallback-protected LLM manager supporting OpenAI, Anthropic, and Ollama.

## Getting Started

### Prerequisites

- Python 3.10+
- Node.js 18+
- [uv](https://github.com/astral-sh/uv) (recommended)

### Installation

1. **Clone the repository**:
   ```bash
   git clone https://github.com/mostofashakib/VidQ.git
   cd VidQ
   ```

2. **Setup Environments**:
   - Create `.env` in `backend/` with your `OPENAI_API_KEY` and/or `ANTHROPIC_API_KEY`.

3. **Run the Application**:
   ```bash
   ./run.sh
   ```

## License

This project is licensed under the **MIT License**. See the [LICENSE](LICENSE) file for details.

---
Created by Mostofa Shakib
