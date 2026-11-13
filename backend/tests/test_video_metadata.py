"""Tests for a downloaded video's title, description, thumbnail and length."""

import pytest

from app.services.scraper.metadata import page_metadata, video_metadata

STATED = """<html><head><title>Deep Sea Creatures - Tube</title>
<meta property="og:title" content="Deep Sea Creatures">
<meta property="og:description" content="Rare footage from the midnight zone.">
<meta property="og:image" content="https://cdn.example/t/42.jpg">
<script type="application/ld+json">{"@type": "VideoObject", "name": "Deep Sea Creatures (Full)",
  "description": "A full documentary.", "thumbnailUrl": ["https://cdn.example/ld/42.jpg"], "duration": "PT52M10S"}
</script></head><body><h1>Deep Sea Creatures</h1></body></html>"""


def test_the_pages_own_video_facts_come_first():
    assert page_metadata(STATED, "https://tube.example/v/42") == {
        "title": "Deep Sea Creatures (Full)",
        "description": "A full documentary.",
        "thumbnail": "https://cdn.example/ld/42.jpg",
        "duration": 3130.0,
    }


def test_open_graph_and_the_title_tag_fill_in_without_structured_data():
    html = '<html><head><title>Ocean film</title><meta name="description" content="About the ocean."></head></html>'

    facts = page_metadata(html, "https://tube.example/v/1")

    assert (facts["title"], facts["description"], facts["thumbnail"]) == ("Ocean film", "About the ocean.", "")


class FakeLLM:
    def __init__(self, answer=None, error=None):
        self.answer, self.error, self.calls = answer, error, []

    async def execute_text(self, prompt, think=False):
        self.calls.append((prompt, think))
        if self.error:
            raise self.error
        return self.answer

    async def execute(self, *args, **kwargs):
        raise AssertionError("metadata never needs a screenshot")


@pytest.mark.asyncio
async def test_a_page_that_states_its_title_needs_no_llm_call():
    llm = FakeLLM()

    result = await video_metadata(llm, STATED, "https://tube.example/v/42", "https://host/temp/a.mp4")

    assert result["title"] == "Deep Sea Creatures (Full)"
    assert result["video_url"] == "https://host/temp/a.mp4"
    assert llm.calls == []


@pytest.mark.asyncio
async def test_without_a_stated_title_the_llm_reads_a_short_summary_of_the_page_without_thinking():
    nav = "<nav>" + "".join(f'<a href="/c/{n}">Category {n}</a>' for n in range(300)) + "</nav>"
    html = (f"<html><body>{nav}<script>var x = 1;</script><h2>Whale song recorded</h2>"
            "<p>Researchers recorded a rare call.</p>" + "<p>filler text</p>" * 2000 + "</body></html>")
    llm = FakeLLM({"title": "Whale song recorded", "description": "A rare whale call."})

    result = await video_metadata(llm, html, "https://tube.example/v/7", "https://host/temp/b.mp4")

    (prompt, think), = llm.calls
    assert think is False
    assert "Whale song recorded" in prompt and "Researchers recorded a rare call." in prompt
    assert "Category 1" not in prompt and "var x" not in prompt
    assert len(prompt) < 4000
    assert (result["title"], result["description"]) == ("Whale song recorded", "A rare whale call.")


@pytest.mark.asyncio
async def test_a_failed_llm_call_leaves_the_title_for_the_caller_to_fill():
    result = await video_metadata(FakeLLM(error=RuntimeError("down")), "<html><body><p>x</p></body></html>",
                                  "https://tube.example/v/7", "https://host/temp/c.mp4")

    assert result["title"] == ""
    assert result["video_url"] == "https://host/temp/c.mp4"
