from django.test import TestCase

from .models import DocumentChunk, ResearchSession, ResearchTask, Source
from .services.chunking import chunk_text
from .services.retrieval import retrieve, tokenize


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
