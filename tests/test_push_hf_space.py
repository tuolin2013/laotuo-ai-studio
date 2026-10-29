"""scripts/push_hf_space.py：Space 仓库只放运行需要的文件，Dockerfile / README 换成 Space 版，不碰工作区。

用本地裸仓库代替 Hugging Face Space，不联网。
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
_spec = importlib.util.spec_from_file_location("push_hf_space", SCRIPTS / "push_hf_space.py")
hf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hf)

FAKE_KEY = "sk-" + "Space3" * 7


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=check)


def put(root: Path, path: str, data: bytes) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def show(git_dir: Path, path: str, ref: str = "main") -> str:
    return git(git_dir, "show", f"{ref}:{path}").stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for key in ("GIT_AUTHOR", "GIT_COMMITTER"):
        monkeypatch.setenv(f"{key}_NAME", "tester")
        monkeypatch.setenv(f"{key}_EMAIL", "tester@example.com")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("LAOTUO_GIT_PROXY", "off")
    monkeypatch.setattr(hf.sync, "MODAL_TOML", tmp_path / "no-modal.toml")

    space = tmp_path / "space.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(space))
    # Space 新建时自带的 .gitattributes 与 README
    seed = tmp_path / "seed"
    git(tmp_path, "init", "-q", "-b", "main", "seed")
    put(seed, ".gitattributes", b"*.png filter=xet diff=xet merge=xet -text\n")
    put(seed, "README.md", b"---\nsdk: docker\n---\n")
    git(seed, "add", ".")
    git(seed, "commit", "-qm", "initial commit")
    git(seed, "push", "-q", str(space), "main")

    root = tmp_path / "work"
    git(tmp_path, "init", "-q", "-b", "master", "work")
    git(root, "config", "core.autocrlf", "false")
    put(root, ".gitignore", b".env\nmodal/\n")
    put(root, "app/main.py", b"print('hi')\r\n")
    put(root, "config/categories/tea.json", b"{}\n")
    put(root, "pyproject.toml", b"[project]\nname = 'x'\n")
    put(root, "LICENSE", b"Apache-2.0\n")
    put(root, "README.md", b"# GitHub README\n")
    put(root, "tests/test_x.py", b"def test_x():\n    pass\n")
    put(root, "docs/guide.md", b"doc\n")
    put(root, "deploy/hf_space/Dockerfile", b"FROM python:3.11-slim\n")
    put(root, "deploy/hf_space/Dockerfile.dockerignore", b".git\n")
    put(root, "deploy/hf_space/README.md", b"---\nsdk: docker\napp_port: 7860\n---\n")
    put(root, "deploy/vercel-landing/index.html", b"<!doctype html>\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "init")
    put(root, ".env", f"RIGHTAPI_LLM_KEY={FAKE_KEY}\n".encode())
    put(root, "modal/h3.py", b"x = 1\n")
    put(root, "app/uncommitted.py", b"x = 2\n")  # 没提交的改动不上线
    git(root, "remote", "add", "hf", str(space))
    return root, space


def test_push_builds_space_layout_without_touching_worktree(repo):
    root, space = repo
    before = (git(root, "status", "--porcelain", "--ignored").stdout, git(root, "ls-files", "--stage").stdout)

    msg = hf.push(root)

    files = set(git(space, "ls-tree", "-r", "--name-only", "main").stdout.split())
    assert files == {"app/main.py", "config/categories/tea.json", "pyproject.toml", "LICENSE", "Dockerfile",
                     ".dockerignore", "README.md", ".gitattributes"}
    assert show(space, "Dockerfile") == "FROM python:3.11-slim\n"
    assert "app_port: 7860" in show(space, "README.md")  # Space 版 README，不是 GitHub 的
    assert "xet" in show(space, ".gitattributes")  # Space 原有的保留
    blob = subprocess.run(["git", "cat-file", "-p", "main:app/main.py"], cwd=space, capture_output=True).stdout
    assert blob == b"print('hi')\r\n"  # 按提交的字节原样上线
    # 接在 Space 原有提交后面（不强推）
    assert git(space, "rev-list", "--count", "main").stdout.strip() == "2" and "已推送" in msg
    assert (git(root, "status", "--porcelain", "--ignored").stdout, git(root, "ls-files", "--stage").stdout) == before
    assert "已是最新" in hf.push(root)  # 没有变化时不重复提交


def test_dry_run_and_missing_remote(repo):
    root, space = repo
    assert "--dry-run" in hf.push(root, dry_run=True)
    assert git(space, "rev-list", "--count", "main").stdout.strip() == "1"
    git(root, "remote", "remove", "hf")
    with pytest.raises(hf.sync.SyncError, match="git remote add hf"):
        hf.push(root)


def test_push_blocks_secrets(repo):
    root, space = repo
    put(root, "app/leak.py", f"KEY = '{FAKE_KEY}'\n".encode())
    git(root, "add", "app/leak.py")
    git(root, "commit", "-qm", "oops")
    with pytest.raises(hf.sync.SyncError, match=r"app/leak\.py"):
        hf.push(root)
    assert git(space, "rev-list", "--count", "main").stdout.strip() == "1"
