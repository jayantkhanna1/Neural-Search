"""Data model for research sessions, sources, RAG chunks and chat history."""

from django.db import models


class ResearchSession(models.Model):
    """A conversation-scoped knowledge base built from one or more research runs."""

    title = models.CharField(max_length=512)
    summary = models.TextField(blank=True, default="")
    # Key claims extracted from the research with cross-source corroboration:
    # [{"claim": str, "confidence": str, "source_urls": [str, ...]}, ...]
    claims = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return f"Session #{self.pk}: {self.title[:60]}"


class ResearchTask(models.Model):
    """A single background research run (query expansion -> search -> filter -> summarize)."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        EXPANDING = "expanding", "Expanding queries"
        SEARCHING = "searching", "Searching the web"
        FETCHING = "fetching", "Fetching sources"
        FILTERING = "filtering", "Filtering for relevance"
        SUMMARIZING = "summarizing", "Summarizing findings"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    session = models.ForeignKey(ResearchSession, on_delete=models.CASCADE, related_name="tasks")
    query = models.CharField(max_length=1024)
    expanded_queries = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.PENDING)
    progress = models.PositiveSmallIntegerField(default=0)
    stage_detail = models.CharField(max_length=512, blank=True, default="")
    error = models.TextField(blank=True, default="")
    summary = models.TextField(blank=True, default="")
    celery_task_id = models.CharField(max_length=128, blank=True, default="")
    urls_found = models.PositiveIntegerField(default=0)
    sources_fetched = models.PositiveIntegerField(default=0)
    sources_kept = models.PositiveIntegerField(default=0)
    rounds_completed = models.PositiveSmallIntegerField(default=0)
    tokens_used = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]

    @property
    def is_active(self) -> bool:
        return self.status not in (self.Status.COMPLETED, self.Status.FAILED)

    def set_stage(self, status: str, detail: str = "", progress: int | None = None):
        """Persist a pipeline stage transition (called from the Celery worker)."""
        self.status = status
        self.stage_detail = detail
        if progress is not None:
            self.progress = min(100, max(0, progress))
        self.save(update_fields=["status", "stage_detail", "progress"])

    def __str__(self):
        return f"Task #{self.pk} [{self.status}] {self.query[:60]}"


class Source(models.Model):
    """A fetched, cleaned and relevance-approved web source."""

    session = models.ForeignKey(ResearchSession, on_delete=models.CASCADE, related_name="sources")
    task = models.ForeignKey(ResearchTask, on_delete=models.CASCADE, related_name="sources")
    url = models.URLField(max_length=2000)
    domain = models.CharField(max_length=255, blank=True, default="")
    title = models.CharField(max_length=1024, blank=True, default="")
    content = models.TextField()
    content_hash = models.CharField(max_length=64, db_index=True)
    summary = models.TextField(blank=True, default="")
    relevance_score = models.FloatField(default=0.0)
    quality_score = models.FloatField(default=0.0)
    published_at = models.DateTimeField(null=True, blank=True)
    fetched_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-quality_score"]
        constraints = [
            models.UniqueConstraint(fields=["session", "url"], name="unique_source_per_session"),
        ]

    def __str__(self):
        return f"{self.title or self.url}"


class DocumentChunk(models.Model):
    """A retrieval unit for RAG, produced by chunking a source's content."""

    session = models.ForeignKey(ResearchSession, on_delete=models.CASCADE, related_name="chunks")
    source = models.ForeignKey(Source, on_delete=models.CASCADE, related_name="chunks")
    index = models.PositiveIntegerField(default=0)
    text = models.TextField()
    # Model-written situating sentence prepended at retrieval/index time
    # (contextual retrieval); empty when disabled or generation failed.
    context = models.TextField(blank=True, default="")
    # Voyage AI embedding of (context + text); null when embeddings disabled.
    embedding = models.JSONField(null=True, blank=True)

    class Meta:
        ordering = ["source_id", "index"]

    def __str__(self):
        return f"Chunk {self.source_id}/{self.index}"


class ChatMessage(models.Model):
    """One turn of the chat-with-research conversation."""

    class Role(models.TextChoices):
        USER = "user", "User"
        ASSISTANT = "assistant", "Assistant"

    session = models.ForeignKey(ResearchSession, on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(max_length=16, choices=Role.choices)
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"[{self.role}] {self.content[:60]}"
