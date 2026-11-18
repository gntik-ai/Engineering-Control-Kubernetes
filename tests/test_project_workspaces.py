from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from engineering_control.db.base import Base
from engineering_control.db.models import (
    BacklogItem,
    ProjectEpic,
    ProjectIssueStory,
    ProjectRecord,
    ProjectStoryEpic,
    ProjectUserStory,
)


def test_planning_links_reuse_issues_and_cannot_cross_project_boundaries(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'planning.db'}")
    Base.metadata.create_all(engine)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys = ON")
            connection.commit()
            with Session(connection) as session:
                projects = [
                    ProjectRecord(
                        name=name,
                        display_name=name,
                        source_repository=f"gntik-ai/{name}",
                        deployment_repository=f"gntik-ai/{name}",
                        origin="console",
                    )
                    for name in ("alpha", "beta")
                ]
                session.add_all(projects)
                session.flush()
                alpha, beta = (project.id for project in projects)
                epics = [
                    ProjectEpic(project_id=project, title=title)
                    for project, title in [(alpha, "Setup"), (alpha, "Usage"), (beta, "Layout")]
                ]
                stories = [
                    ProjectUserStory(project_id=project, title=title)
                    for project, title in [
                        (alpha, "First run"),
                        (alpha, "Usage view"),
                        (beta, "Menu"),
                    ]
                ]
                item = BacklogItem(
                    project_id=alpha,
                    title="Record first run",
                    source="github",
                    state="done",
                    issue_state="closed",
                    repository="gntik-ai/alpha",
                    issue_number=17,
                )
                session.add_all([*epics, *stories, item])
                session.flush()
                setup, usage, layout = epics
                first_run, usage_view, menu = stories
                item_id = item.id
                session.add_all(
                    [
                        ProjectStoryEpic(project_id=alpha, story_id=first_run.id, epic_id=setup.id),
                        ProjectStoryEpic(project_id=alpha, story_id=first_run.id, epic_id=usage.id),
                        ProjectIssueStory(project_id=alpha, item_id=item_id, story_id=first_run.id),
                        ProjectIssueStory(
                            project_id=alpha, item_id=item_id, story_id=usage_view.id
                        ),
                    ]
                )
                session.commit()

                for wrong_link in (
                    ProjectStoryEpic(project_id=alpha, story_id=first_run.id, epic_id=layout.id),
                    ProjectStoryEpic(project_id=alpha, story_id=menu.id, epic_id=setup.id),
                    ProjectIssueStory(project_id=alpha, item_id=item_id, story_id=menu.id),
                    ProjectIssueStory(project_id=beta, item_id=item_id, story_id=menu.id),
                ):
                    with pytest.raises(IntegrityError), session.begin_nested():
                        session.add(wrong_link)
                assert session.scalar(select(func.count()).select_from(ProjectStoryEpic)) == 2
                assert session.scalar(select(func.count()).select_from(ProjectIssueStory)) == 2
                saved = session.get(BacklogItem, item_id)
                assert saved is not None
                assert (saved.state, saved.issue_state, saved.issue_number) == (
                    "done",
                    "closed",
                    17,
                )
    finally:
        engine.dispose()
