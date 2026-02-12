"""FastAPI + SQLAlchemy stack test app.

This app models a small project-tracking service with:
- projects owned by a user
- tasks attached to projects
- simple status and dashboard endpoints
"""

from __future__ import annotations

import os
from collections import Counter
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, create_engine, func, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

from drift import TuskDrift

sdk = TuskDrift.initialize(
    api_key="tusk-test-key",
    log_level="debug",
)


def _db_url() -> str:
    host = os.getenv("POSTGRES_HOST", "postgres")
    port = os.getenv("POSTGRES_PORT", "5432")
    db = os.getenv("POSTGRES_DB", "testdb")
    user = os.getenv("POSTGRES_USER", "testuser")
    password = os.getenv("POSTGRES_PASSWORD", "testpass")
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{db}"


engine = create_engine(_db_url(), future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    projects: Mapped[list[Project]] = relationship(back_populates="owner")


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    owner: Mapped[User] = relationship(back_populates="projects")
    tasks: Mapped[list[Task]] = relationship(back_populates="project", cascade="all,delete-orphan")


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="todo")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    project: Mapped[Project] = relationship(back_populates="tasks")


class CreateProjectRequest(BaseModel):
    owner_email: str
    owner_name: str = Field(min_length=2, max_length=120)
    name: str = Field(min_length=3, max_length=200)


class CreateTaskRequest(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    description: Optional[str] = None
    status: str = Field(default="todo")


class UpdateTaskStatusRequest(BaseModel):
    status: str


class ProjectSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    project_id: int
    project_name: str
    owner_email: str
    total_tasks: int
    status_counts: dict[str, int]
    open_task_titles: list[str]


app = FastAPI(title="FastAPI + SQLAlchemy Project Tracker")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.on_event("startup")
def startup() -> None:
    # Schema creation/reset is handled in entrypoint setup so startup remains
    # free of extra DB queries that can interfere with replay matching.
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    sdk.mark_app_as_ready()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "healthy"}


@app.get("/api/diagnostics/db-ready")
def db_ready(db: Session = Depends(get_db)) -> dict[str, bool]:
    # Fire a trivial SQL query to exercise SQLAlchemy instrumentation, but keep
    # response deterministic even if replay returns an empty mocked row set.
    db.execute(text("SELECT 1")).all()
    return {"db_ready": True}


@app.post("/api/projects", status_code=201)
def create_project(payload: CreateProjectRequest, db: Session = Depends(get_db)) -> dict[str, object]:
    owner = db.execute(select(User).where(User.email == payload.owner_email)).scalar_one_or_none()
    if owner is None:
        owner = User(name=payload.owner_name, email=payload.owner_email, created_at=_utcnow())
        db.add(owner)
        db.flush()

    project = Project(name=payload.name, owner_id=owner.id, created_at=_utcnow())
    db.add(project)
    db.commit()

    return {
        "id": project.id,
        "name": project.name,
        "owner_email": owner.email,
    }


@app.post("/api/projects/{project_id}/tasks", status_code=201)
def create_task(project_id: int, payload: CreateTaskRequest, db: Session = Depends(get_db)) -> dict[str, object]:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    task = Task(
        project_id=project_id,
        title=payload.title,
        description=payload.description,
        status=payload.status,
        created_at=_utcnow(),
        updated_at=_utcnow(),
    )
    db.add(task)
    db.commit()

    return {
        "id": task.id,
        "project_id": task.project_id,
        "title": task.title,
        "status": task.status,
    }


@app.patch("/api/tasks/{task_id}/status")
def update_task_status(
    task_id: int, payload: UpdateTaskStatusRequest, db: Session = Depends(get_db)
) -> dict[str, object]:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    task.status = payload.status
    task.updated_at = _utcnow()
    db.commit()

    return {"id": task.id, "status": task.status}


@app.get("/api/projects/{project_id}/summary", response_model=ProjectSummaryResponse)
def project_summary(project_id: int, db: Session = Depends(get_db)) -> ProjectSummaryResponse:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    rows = db.execute(select(Task.status, Task.title).where(Task.project_id == project_id)).all()
    counts = Counter([row[0] for row in rows])
    open_titles = [row[1] for row in rows if row[0] != "done"][:5]

    return ProjectSummaryResponse(
        project_id=project.id,
        project_name=project.name,
        owner_email=project.owner.email,
        total_tasks=len(rows),
        status_counts=dict(counts),
        open_task_titles=open_titles,
    )


@app.post("/api/projects/{project_id}/bulk-close")
def bulk_close_project_tasks(
    project_id: int,
    statuses: str = Query(default="todo,in_progress"),
    db: Session = Depends(get_db),
) -> dict[str, int]:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    target_statuses = [s.strip() for s in statuses.split(",") if s.strip()]
    if not target_statuses:
        raise HTTPException(status_code=400, detail="No statuses provided")

    now = _utcnow()
    count = (
        db.query(Task)
        .filter(Task.project_id == project_id, Task.status.in_(target_statuses))
        .update({"status": "done", "updated_at": now}, synchronize_session=False)
    )
    db.commit()

    return {"project_id": project_id, "updated_count": int(count)}


@app.get("/api/dashboard")
def dashboard(owner_email: str, db: Session = Depends(get_db)) -> dict[str, object]:
    owner = db.execute(select(User).where(User.email == owner_email)).scalar_one_or_none()
    if owner is None:
        raise HTTPException(status_code=404, detail="Owner not found")

    project_count = db.execute(select(func.count(Project.id)).where(Project.owner_id == owner.id)).scalar_one()
    task_count = db.execute(
        select(func.count(Task.id)).join(Project, Project.id == Task.project_id).where(Project.owner_id == owner.id)
    ).scalar_one()
    done_count = db.execute(
        select(func.count(Task.id))
        .join(Project, Project.id == Task.project_id)
        .where(Project.owner_id == owner.id, Task.status == "done")
    ).scalar_one()

    return {
        "owner_email": owner.email,
        "projects": int(project_count or 0),
        "tasks": int(task_count or 0),
        "done_tasks": int(done_count or 0),
    }


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
