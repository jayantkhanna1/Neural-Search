from django.contrib import admin

from .models import ChatMessage, DocumentChunk, ResearchSession, ResearchTask, Source


@admin.register(ResearchSession)
class ResearchSessionAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "created_at", "updated_at")
    search_fields = ("title",)


@admin.register(ResearchTask)
class ResearchTaskAdmin(admin.ModelAdmin):
    list_display = ("id", "session", "query", "status", "progress", "sources_kept", "created_at")
    list_filter = ("status",)
    search_fields = ("query",)


@admin.register(Source)
class SourceAdmin(admin.ModelAdmin):
    list_display = ("id", "session", "title", "domain", "relevance_score", "quality_score")
    search_fields = ("title", "url", "domain")


@admin.register(DocumentChunk)
class DocumentChunkAdmin(admin.ModelAdmin):
    list_display = ("id", "session", "source", "index")


@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    list_display = ("id", "session", "role", "created_at")
    list_filter = ("role",)
