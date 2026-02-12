"""Execute test requests against the FastAPI + SQLAlchemy app."""

from drift.instrumentation.e2e_common.test_utils import make_request, print_request_summary


def expect_status(response, expected_status: int) -> None:
    if response.status_code != expected_status:
        raise RuntimeError(
            f"Unexpected status: got {response.status_code}, expected {expected_status}. Body: {response.text[:500]}"
        )


if __name__ == "__main__":
    print("Starting FastAPI + SQLAlchemy project-tracker request sequence...\n")

    # Basic service and DB checks.
    expect_status(make_request("GET", "/health"), 200)
    expect_status(make_request("GET", "/api/diagnostics/db-ready"), 200)

    # Read seeded data paths.
    expect_status(make_request("GET", "/api/projects/1/summary"), 200)
    expect_status(make_request("GET", "/api/dashboard", params={"owner_email": "pm@example.com"}), 200)

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
    expect_status(create_project_resp, 201)
    project_id = create_project_resp.json()["id"]

    # Write path: create task.
    create_task_resp = make_request(
        "POST",
        f"/api/projects/{project_id}/tasks",
        json={
            "title": "Write rollback plan",
            "description": "Document failure modes and mitigation.",
            "status": "todo",
        },
    )
    expect_status(create_task_resp, 201)
    task_id = create_task_resp.json()["id"]

    # Write path: mutate task.
    expect_status(
        make_request(
            "PATCH",
            f"/api/tasks/{task_id}/status",
            json={"status": "in_progress"},
        ),
        200,
    )

    # Write path: bulk update.
    expect_status(
        make_request(
            "POST",
            f"/api/projects/{project_id}/bulk-close",
            params={"statuses": "todo,in_progress"},
        ),
        200,
    )

    # Read back aggregate state after mutations.
    expect_status(make_request("GET", f"/api/projects/{project_id}/summary"), 200)
    expect_status(make_request("GET", "/api/dashboard", params={"owner_email": "owner@example.com"}), 200)

    # Repeat health checks to keep previous baseline requests too.
    expect_status(make_request("GET", "/api/diagnostics/db-ready"), 200)
    expect_status(make_request("GET", "/health"), 200)

    print_request_summary()
