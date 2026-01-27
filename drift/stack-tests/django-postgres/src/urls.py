"""URL configuration for Django + PostgreSQL test."""

import views
from django.urls import path

urlpatterns = [
    path("health", views.health, name="health"),
    # Database operations
    path("db/query", views.db_query, name="db_query"),
    path("db/insert", views.db_insert, name="db_insert"),
    path("db/update/<int:user_id>", views.db_update, name="db_update"),
    path("db/delete/<int:user_id>", views.db_delete, name="db_delete"),
    # Integration-specific tests
    path("db/register-jsonb", views.db_register_jsonb, name="db_register_jsonb"),
    path("db/transaction", views.db_transaction, name="db_transaction"),
    path("db/raw-connection", views.db_raw_connection, name="db_raw_connection"),
]
