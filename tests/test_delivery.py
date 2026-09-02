from __future__ import annotations

from media_transcription_bot.delivery import transcript_document
from media_transcription_bot.models import TranscriptArtifact


def test_transcript_document_includes_provenance():
    artifact = TranscriptArtifact(
        title="Example",
        source_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        transcript="hello",
        transcript_source="youtube",
        summary_context="Title: Example",
        filename_stem="youtube-dQw4w9WgXcQ",
        metadata={"Video ID": "dQw4w9WgXcQ"},
    )
    document = transcript_document(artifact)
    assert "Transcript source: youtube" in document
    assert "Video ID: dQw4w9WgXcQ" in document
    assert document.endswith("Transcript:\nhello")
