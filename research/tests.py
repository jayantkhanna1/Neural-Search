from datetime import datetime, timedelta, timezone

from django.test import TestCase

from .models import DocumentChunk, ResearchSession, ResearchTask, Source
from .services.chunking import chunk_text
from .services.relevance import quality_score
from .services.retrieval import retrieve, tokenize
from .services.scraper import _parse_date


class ChunkingTests(TestCase):
    def test_short_text_single_chunk(self):
        self.assertEqual(chunk_text("hello world"), ["hello world"])

    def test_empty_text(self):
        self.assertEqual(chunk_text("   "), [])

    def test_long_text_overlapping_chunks(self):
        text = " ".join(f"word{i}" for i in range(2000))
        chunks = chunk_text(text, chunk_size=500, overlap=100)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 600 for c in chunks))
        # Every chunk except possibly the last should be non-trivial
        self.assertTrue(all(chunks))


class RetrievalTests(TestCase):
    def setUp(self):
        self.session = ResearchSession.objects.create(title="quantum computing")
        self.task = ResearchTask.objects.create(session=self.session, query="quantum computing")

    def _add_source(self, url: str, text: str):
        source = Source.objects.create(
            session=self.session,
            task=self.task,
            url=url,
            content=text,
            content_hash=url,
        )
        DocumentChunk.objects.create(session=self.session, source=source, index=0, text=text)

    def test_tokenize_removes_stopwords(self):
        self.assertEqual(tokenize("What is the speed of light"), ["speed", "light"])

    def test_retrieval_ranks_relevant_chunk_first(self):
        self._add_source(
            "https://a.example/qubits",
            "Qubits are the fundamental unit of quantum computing and enable superposition.",
        )
        self._add_source(
            "https://b.example/cooking",
            "This recipe describes how to bake sourdough bread with a crispy crust.",
        )
        hits = retrieve(self.session.id, "what are qubits in quantum computing", top_k=2)
        self.assertTrue(hits)
        self.assertIn("Qubits", hits[0].chunk.text)

    def test_retrieval_empty_session(self):
        empty = ResearchSession.objects.create(title="empty")
        self.assertEqual(retrieve(empty.id, "anything", top_k=5), [])


class QualityScoreTests(TestCase):
    def test_fresh_source_outranks_stale_source(self):
        now = datetime.now(timezone.utc)
        fresh = quality_score("https://example.org/a", 0.8, 5000, now - timedelta(days=10))
        stale = quality_score("https://example.org/b", 0.8, 5000, now - timedelta(days=1500))
        undated = quality_score("https://example.org/c", 0.8, 5000, None)
        self.assertGreater(fresh, stale)
        self.assertGreaterEqual(fresh, undated)

    def test_score_bounded(self):
        score = quality_score("https://arxiv.org/abs/1", 1.0, 100000, datetime.now(timezone.utc))
        self.assertLessEqual(score, 1.0)


class DateParsingTests(TestCase):
    def test_iso_datetime(self):
        parsed = _parse_date("2026-03-14T09:26:53Z")
        self.assertEqual((parsed.year, parsed.month, parsed.day), (2026, 3, 14))
        self.assertIsNotNone(parsed.tzinfo)

    def test_date_prefix(self):
        parsed = _parse_date("2025-12-01 some trailing junk")
        self.assertEqual((parsed.year, parsed.month, parsed.day), (2025, 12, 1))

    def test_garbage_returns_none(self):
        self.assertIsNone(_parse_date("not a date"))
        self.assertIsNone(_parse_date(""))


class ContextualRetrievalTests(TestCase):
    def test_context_participates_in_matching(self):
        session = ResearchSession.objects.create(title="t")
        task = ResearchTask.objects.create(session=session, query="t")
        source = Source.objects.create(
            session=session, task=task, url="https://x.example/1",
            content="body", content_hash="h1",
        )
        DocumentChunk.objects.create(
            session=session, source=source, index=0,
            text="The measured value was 4.2 units in the trial.",
            context="This excerpt discusses zeppelin cargo capacity results.",
        )
        hits = retrieve(session.id, "zeppelin cargo capacity", top_k=3)
        self.assertTrue(hits)


class ApiTests(TestCase):
    def test_index_renders(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)

    def test_create_session_requires_query(self):
        response = self.client.post("/api/research/", data="{}", content_type="application/json")
        self.assertEqual(response.status_code, 400)

    def test_session_detail_404(self):
        response = self.client.get("/api/sessions/9999/")
        self.assertEqual(response.status_code, 404)
