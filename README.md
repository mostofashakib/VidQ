# VidQ

A high-performance, agentic video extraction and search platform. VidQ uses LLM-guided vision to navigate complex video hosting sites and captures high-quality content via browser native MediaRecorder.

## Features

- **Agentic Navigation**: Uses Vision LLMs (OpenAI/Anthropic) to bypass overlays, age-gates, and interact with custom video players.
- **Unified Extraction**: Integrated MediaRecorder pipeline for capturing 720p+ video with synchronized audio.
- **Local Storage**: Automatically manages extracted content in a local `temp_storage` for immediate preview.
- **Glassmorphism UI**: Simple, sleek, and modern web interface built with Next.js.
- **Resilient Pipeline**: Smart recovery loops for blocked interactions and CORS-bypass for cross-domain captures.

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
   git clone https://github.com/adibshakib/vidQ.git
   cd vidQ
   ```

2. **Setup Environments**:
   - Create `.env` in `backend/` with your `OPENAI_API_KEY` and `ANTHROPIC_API_KEY`.

3. **Run the Application**:
   Use the provided orchestration script:
   ```bash
   ./run.sh
   ```

## License

This project is licensed under the **MIT License**. See the [LICENSE](LICENSE) file for details.

---
Created by Mostofa Shakib
