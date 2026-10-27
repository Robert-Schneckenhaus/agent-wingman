"""Data access for workspaces and repositories."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Connection, func, insert, select

from wingman.storage.schema import files, repositories, workspaces


class StoreError(Exception):
    pass


@dataclass(frozen=True)
class RepoInfo:
    id: int
    name: str
    root_path: str
    remote_url: str | None
    last_commit: str | None
    created_at: datetime
    file_count: int = 0


def ensure_workspace(conn: Connection, name: str) -> int:
    existing = conn.scalar(select(workspaces.c.id).where(workspaces.c.name == name))
    if existing is not None:
        return existing
    return conn.execute(insert(workspaces).values(name=name)).inserted_primary_key[0]


def get_workspace_id(conn: Connection, name: str) -> int:
    workspace_id = conn.scalar(select(workspaces.c.id).where(workspaces.c.name == name))
    if workspace_id is None:
        raise StoreError(f"Workspace '{name}' does not exist. Run 'wingman init'.")
    return workspace_id


def add_repository(
    conn: Connection,
    workspace_id: int,
    name: str,
    root_path: str,
    remote_url: str | None = None,
) -> int:
    for column, value, label in (
        (repositories.c.name, name, "name"),
        (repositories.c.root_path, root_path, "path"),
    ):
        clash = conn.scalar(
            select(repositories.c.name).where(
                repositories.c.workspace_id == workspace_id, column == value
            )
        )
        if clash is not None:
            raise StoreError(f"A repository with this {label} already exists: '{clash}'.")

    result = conn.execute(
        insert(repositories).values(
            workspace_id=workspace_id,
            name=name,
            root_path=root_path,
            remote_url=remote_url,
        )
    )
    return result.inserted_primary_key[0]


def list_repositories(conn: Connection, workspace_id: int) -> list[RepoInfo]:
    file_count = (
        select(func.count(files.c.id)).where(files.c.repo_id == repositories.c.id).scalar_subquery()
    )
    rows = conn.execute(
        select(
            repositories.c.id,
            repositories.c.name,
            repositories.c.root_path,
            repositories.c.remote_url,
            repositories.c.last_commit,
            repositories.c.created_at,
            file_count.label("file_count"),
        )
        .where(repositories.c.workspace_id == workspace_id)
        .order_by(repositories.c.name)
    )
    return [RepoInfo(**row._mapping) for row in rows]


def remove_repository(conn: Connection, workspace_id: int, name: str) -> None:
    result = conn.execute(
        repositories.delete().where(
            repositories.c.workspace_id == workspace_id, repositories.c.name == name
        )
    )
    if result.rowcount == 0:
        raise StoreError(f"No repository named '{name}'.")
