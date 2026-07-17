"""URL configuration for the Neural Search project."""

from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("", include("research.urls")),
    path("admin/", admin.site.urls),
]
