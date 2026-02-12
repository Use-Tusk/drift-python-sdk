"""Execute test requests against the FastAPI + SQLAlchemy app."""

from drift.instrumentation.e2e_common.test_utils import make_request, print_request_summary

if __name__ == "__main__":
    print("Starting FastAPI + SQLAlchemy project-tracker request sequence...\n")

    # Basic service and DB checks.
    make_request("GET", "/health")
    make_request("GET", "/api/diagnostics/db-ready")

    # Read seeded data paths.
    make_request("GET", "/api/projects/1/summary")
    make_request("GET", "/api/dashboard", params={"owner_email": "pm@example.com"})

    # Write path: create owner/project.
    create_project_resp = make_request(
        "POST",
        "/api/projects",
        json={
            "owner_email": "owner@example.com",
            "owner_name": "Owner Example",
            "name": "Replay Hardening Program",
        },
    )
    project_id = None
    if create_project_resp.status_code == 201:
        try:
            project_id = create_project_resp.json().get("id")
        except Exception:
            project_id = None

    # Write path: create task.
    task_id = None
    create_task_resp = None
    if project_id is not None:
        create_task_resp = make_request(
            "POST",
            f"/api/projects/{project_id}/tasks",
            json={
                "title": "Write rollback plan",
                "description": "Document failure modes and mitigation.",
                "status": "todo",
            },
        )
        if create_task_resp.status_code == 201:
            try:
                task_id = create_task_resp.json().get("id")
            except Exception:
                task_id = None

    # Write path: mutate task.
    if task_id is not None:
        make_request(
            "PATCH",
            f"/api/tasks/{task_id}/status",
            json={"status": "in_progress"},
        )

    # Write path: bulk update.
    if project_id is not None:
        make_request(
            "POST",
            f"/api/projects/{project_id}/bulk-close",
            params={"statuses": "todo,in_progress"},
        )

    # Read back aggregate state after mutations.
    if project_id is not None:
        make_request("GET", f"/api/projects/{project_id}/summary")
    make_request("GET", "/api/dashboard", params={"owner_email": "owner@example.com"})

    # Repeat health checks to keep previous baseline requests too.
    make_request("GET", "/api/diagnostics/db-ready")
    make_request("GET", "/health")

    print_request_summary()
