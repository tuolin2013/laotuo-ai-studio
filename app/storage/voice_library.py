"""音色库：workspace/voice_library/，所有项目共用，让同一品牌的配音始终是同一个声音。

每个音色一个子目录（样音 sample.wav），索引写在 library.json（先写临时文件再原子替换）。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
from pathlib import Path

from app.schemas.voice import VoiceProfile


class VoiceLibrary:
    def __init__(self, workspace_dir: Path):
        self.root = Path(workspace_dir) / "voice_library"
        self.index = self.root / "library.json"
        self._lock = threading.Lock()

    def _read(self) -> list[VoiceProfile]:
        if not self.index.is_file():
            return []
        try:
            raw = json.loads(self.index.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        out = []
        for item in raw if isinstance(raw, list) else []:
            try:
                out.append(VoiceProfile.model_validate(item))
            except ValueError:
                continue  # 损坏的条目不影响其他音色
        return out

    def _write(self, profiles: list[VoiceProfile]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        data = json.dumps([p.model_dump(mode="json") for p in profiles], ensure_ascii=False, indent=2)
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".library.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
            os.replace(tmp, self.index)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def list(self) -> list[VoiceProfile]:
        """样音文件仍在的音色，最新的排在前面。"""
        with self._lock:
            profiles = [p for p in self._read() if p.sample_path.exists()]
        return sorted(profiles, key=lambda p: p.created_at, reverse=True)

    def get(self, voice_id: str | None) -> VoiceProfile:
        for p in self.list():
            if p.voice_id == voice_id:
                return p
        raise ValueError("音色库里找不到这个音色（可能已被删除），请重新选择")

    def add(self, name: str, kind: str, sample: Path, *, prompt_text: str = "", description: str = "",
            language: str = "mandarin", sample_seconds: float = 0.0) -> VoiceProfile:
        """复制样音进音色库并登记。同名音色允许并存（以 voice_id 区分）。"""
        draft = VoiceProfile(name=name, kind=kind, description=description, sample_path=sample,
                             prompt_text=prompt_text, language=language, sample_seconds=sample_seconds)
        folder = self.root / draft.voice_id
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / f"sample{Path(sample).suffix or '.wav'}"
        shutil.copy2(sample, dest)
        profile = draft.model_copy(update={"sample_path": dest})
        with self._lock:
            self._write([*self._read(), profile])
        return profile

    def delete(self, voice_id: str) -> VoiceProfile:
        with self._lock:
            profiles = self._read()
            keep = [p for p in profiles if p.voice_id != voice_id]
            gone = [p for p in profiles if p.voice_id == voice_id]
            if not gone:
                raise ValueError("音色库里找不到这个音色")
            self._write(keep)
        folder = self.root / voice_id
        if folder.is_dir() and folder.resolve().is_relative_to(self.root.resolve()):
            shutil.rmtree(folder, ignore_errors=True)
        return gone[0]
