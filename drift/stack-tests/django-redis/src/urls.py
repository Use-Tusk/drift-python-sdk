"""URL configuration for Django + Redis test."""

import views
from django.urls import path

urlpatterns = [
    path("health", views.health, name="health"),
    # Cache operations via Django cache framework
    path("cache/set", views.cache_set, name="cache_set"),
    path("cache/get/<str:key>", views.cache_get, name="cache_get"),
    path("cache/delete/<str:key>", views.cache_delete, name="cache_delete"),
    path("cache/incr/<str:key>", views.cache_incr, name="cache_incr"),
    # Session operations
    path("session/set", views.session_set, name="session_set"),
    path("session/get", views.session_get, name="session_get"),
    path("session/clear", views.session_clear, name="session_clear"),
    # Direct Redis operations
    path("redis/direct", views.redis_direct, name="redis_direct"),
    path("redis/pipeline", views.redis_pipeline, name="redis_pipeline"),
]
