"""URL configuration for Django e2e test application."""

from django.urls import path

from views import (
    health,
    get_weather,
    get_user,
    create_post,
    get_post,
    delete_post,
    get_activity,
)

urlpatterns = [
    path("health", health, name="health"),
    path("api/weather", get_weather, name="get_weather"),
    path("api/user/<str:user_id>", get_user, name="get_user"),
    path("api/post", create_post, name="create_post"),
    path("api/post/<int:post_id>", get_post, name="get_post"),
    path("api/post/<int:post_id>/delete", delete_post, name="delete_post"),
    path("api/activity", get_activity, name="get_activity"),
]

