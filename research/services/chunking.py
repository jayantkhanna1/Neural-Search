"""Split source content into overlapping chunks for RAG retrieval."""

from django.conf import settings


def chunk_text(text: str, chunk_size: int | None = None, overlap: int | None = None) -> list[str]:
    """Split text into chunks of ~chunk_size chars with overlap, preferring
    paragraph and sentence boundaries so chunks stay coherent."""
    chunk_size = chunk_size or settings.RESEARCH["CHUNK_SIZE"]
    overlap = overlap if overlap is not None else settings.RESEARCH["CHUNK_OVERLAP"]
    text = text.strip()
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]

    chunks = []
    start = 0
    length = len(text)
    while start < length:
        end = min(start + chunk_size, length)
        if end < length:
            # Try to break at a paragraph, then sentence, then word boundary.
            window = text[start:end]
            for sep in ("\n\n", "\n", ". ", " "):
                cut = window.rfind(sep)
                if cut > chunk_size // 2:
                    end = start + cut + len(sep)
                    break
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= length:
            break
        start = max(end - overlap, start + 1)
    return chunks
