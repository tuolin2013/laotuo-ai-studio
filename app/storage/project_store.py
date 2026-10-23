"""项目本地持久化：workspace/projects/{project_id}/。"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from app.schemas.project import Project

_SAFE_ID = re.compile(r"^[A-Za-z0-9_\-]+$")
SUBDIRS = ("references", "candidates", "first_frames", "videos", "prompts", "reference_video_frames", "edits",
           "voices")


class ProjectStore:
    def __init__(self, workspace_dir: Path):
        self.root = Path(workspace_dir) / "projects"
        self.root.mkdir(parents=True, exist_ok=True)

    def project_dir(self, project_id: str) -> Path:
        # 防止路径穿越
        if not _SAFE_ID.match(project_id):
            raise ValueError(f"非法的 project_id：{project_id!r}")
        return self.root / project_id

    def save(self, project: Project) -> Path:
        """原子写入 project.json：先写临时文件再替换。"""
        project.touch()
        d = self.project_dir(project.project_id)
        for sub in SUBDIRS:
            (d / sub).mkdir(parents=True, exist_ok=True)
        target = d / "project.json"
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".project.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(project.model_dump_json(indent=2))
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return target

    def load(self, project_id: str) -> Project:
        path = self.project_dir(project_id) / "project.json"
        return Project.model_validate_json(path.read_text(encoding="utf-8"))

    def list_projects(self) -> list[Project]:
        projects = []
        for path in self.root.glob("*/project.json"):
            try:
                projects.append(Project.model_validate_json(path.read_text(encoding="utf-8")))
            except ValueError:
                continue  # 损坏的项目文件不影响其他项目列出
        return sorted(projects, key=lambda p: p.updated_at, reverse=True)

    def append_edit_log(self, project_id: str, record: dict) -> None:
        d = self.project_dir(project_id)
        d.mkdir(parents=True, exist_ok=True)
        with (d / "edit_log.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def asset_dir(self, project_id: str, subdir: str, sub_id: str | None = None) -> Path:
        """项目内素材目录（自动创建），例如 candidates/{slot_id}/。"""
        if subdir not in SUBDIRS:
            raise ValueError(f"未知的素材目录：{subdir}")
        d = self.project_dir(project_id) / subdir
        if sub_id:
            if not _SAFE_ID.match(sub_id):
                raise ValueError(f"非法的子目录 ID：{sub_id!r}")
            d = d / sub_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def import_file(
        self,
        project_id: str,
        src: Path | str,
        subdir: str,
        sub_id: str | None = None,
        dest_name: str | None = None,
    ) -> Path:
        """把上传 / 生成的文件复制进项目目录，返回新路径。"""
        src = Path(src)
        dest = self.asset_dir(project_id, subdir, sub_id) / Path(dest_name or src.name).name
        if src.resolve() != dest.resolve():
            shutil.copy2(src, dest)
        return dest
