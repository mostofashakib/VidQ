"""Tests for the translation prompt and for merging its answers back into subtitles."""

from app.services.translate_worker import _build_translation_prompt, _chunk_segments, _merge_translated_chunks


def seg(start, end, text):
    return {"start": start, "end": end, "text": text}


CHUNK = [seg(0.0, 2.5, " Hola "), seg(2.5, 5.0, "Mucho gusto"), seg(5.0, 7.0, "Adiós")]


def test_the_prompt_sends_numbered_text_without_timings():
    prompt = _build_translation_prompt(CHUNK)

    assert "1| Hola\n2| Mucho gusto\n3| Adiós" in prompt
    assert "-->" not in prompt and "00:00" not in prompt


def test_answers_map_back_to_their_segments_and_keep_the_original_timings():
    answers = ["Sure! Here it is:\n1| Hello\n2|Nice to meet you\n3| Goodbye\n", "1| Thanks"]
    chunks = [CHUNK, [seg(7.0, 9.0, "Gracias")]]

    srt = _merge_translated_chunks(chunks, answers)

    assert srt == (
        "1\n00:00:00,000 --> 00:00:02,500\nHello\n\n"
        "2\n00:00:02,500 --> 00:00:05,000\nNice to meet you\n\n"
        "3\n00:00:05,000 --> 00:00:07,000\nGoodbye\n\n"
        "4\n00:00:07,000 --> 00:00:09,000\nThanks\n"
    )


def test_a_line_the_model_skipped_keeps_its_original_text():
    srt = _merge_translated_chunks([CHUNK], ["1| Hello\n3| Goodbye\n7| Stray line"])

    assert "\nMucho gusto\n" in srt
    assert "Stray line" not in srt


def test_chunks_count_only_the_text_and_a_short_line_number():
    segments = [seg(i, i + 1, "x" * 40) for i in range(300)]  # about 10 tokens of text each

    chunks = _chunk_segments(segments)

    assert len(chunks) == 2
    assert sum(len(c) for c in chunks) == 300
