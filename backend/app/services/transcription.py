import abc
import logging

logger = logging.getLogger("Transcription")


class TranscriptionAdapter(abc.ABC):
    """Abstract adapter for speech-to-text transcription."""

    @abc.abstractmethod
    def transcribe(self, audio_path: str) -> list[dict]:
        """Transcribe audio file. Returns list of {start, end, text} segment dicts."""


class FasterWhisperAdapter(TranscriptionAdapter):
    """Transcription via faster-whisper.

    Model files are downloaded once into `model_dir` and reused on every
    subsequent call — no network access after the first download.
    """

    def __init__(
        self,
        model: str = "large-v3-turbo",
        model_dir: str | None = None,
        device: str = "cpu",
        compute_type: str = "int8",
    ):
        self.model_name = model
        self.model_dir = model_dir  # local cache dir; downloaded here once
        self.device = device
        self.compute_type = compute_type
        self._model = None

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel
            logger.info(
                f"Loading faster-whisper model: {self.model_name} "
                f"from {self.model_dir or 'HuggingFace default cache'} "
                f"(device={self.device}, compute_type={self.compute_type})"
            )
            self._model = WhisperModel(
                self.model_name,
                device=self.device,
                compute_type=self.compute_type,
                download_root=self.model_dir,
            )
        return self._model

    def transcribe(self, audio_path: str) -> list[dict]:
        model = self._load()
        logger.info(f"Transcribing with faster-whisper ({self.model_name})")
        segments, _info = model.transcribe(audio_path)
        return [
            {"start": seg.start, "end": seg.end, "text": seg.text}
            for seg in segments
        ]


class WhisperOpenAIAdapter(TranscriptionAdapter):
    """Transcription via OpenAI's Whisper API."""

    def __init__(self, api_key: str, model: str = "whisper-1"):
        self.api_key = api_key
        self.model = model

    def transcribe(self, audio_path: str) -> list[dict]:
        from openai import OpenAI
        client = OpenAI(api_key=self.api_key)
        logger.info(f"Transcribing with OpenAI Whisper ({self.model})")
        with open(audio_path, "rb") as f:
            transcript = client.audio.transcriptions.create(
                file=f,
                model=self.model,
                response_format="verbose_json",
            )
        return [
            {"start": seg.start, "end": seg.end, "text": seg.text}
            for seg in transcript.segments
        ]
