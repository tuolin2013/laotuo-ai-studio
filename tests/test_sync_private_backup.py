"""一键同步脚本 scripts/sync_private_backup.py：私有目录只进私人仓库，密钥与缓存哪里都不上传。

全部在临时目录里进行，用两个本地裸仓库代替 GitHub 上的公开仓 / 私人仓，不联网。
"""

import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sync_private_backup.py"
_spec = importlib.util.spec_from_file_location("sync_private_backup", SCRIPT)
sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync)

# 拼出来的假 Key，避免本文件自己命中密钥检查
FAKE_KEY = "sk-" + "Test9" * 8

GITIGNORE = """\
.env
.env.*
!.env.example
__pycache__/
workspace/*
!workspace/.gitkeep
modal/
config/skills_pro/
tests/pro/
promo-film/
"""


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=check)


def put(root: Path, path: str, data: bytes) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def tree_files(git_dir: Path, ref: str) -> set[str]:
    out = git(git_dir, "-c", "core.quotepath=off", "ls-tree", "-r", "--name-only", ref).stdout
    return set(out.splitlines())


def head(git_dir: Path, ref: str = "refs/heads/main") -> str | None:
    proc = git(git_dir, "rev-parse", "--verify", "--quiet", ref, check=False)
    return proc.stdout.strip() or None


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """work/ 工作仓库（master 已提交公开代码，磁盘上另有私有目录），origin / private 为空的裸仓库。"""
    for key in ("GIT_AUTHOR", "GIT_COMMITTER"):
        monkeypatch.setenv(f"{key}_NAME", "tester")
        monkeypatch.setenv(f"{key}_EMAIL", "tester@example.com")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")  # 不受本机 autocrlf / 凭据管理器影响
    monkeypatch.setenv("LAOTUO_GIT_PROXY", "off")
    monkeypatch.setattr(sync, "MODAL_TOML", tmp_path / "no-modal.toml")

    for bare in ("public.git", "private.git"):
        git(tmp_path, "init", "-q", "--bare", "-b", "main", bare)
    root = tmp_path / "work"
    git(tmp_path, "init", "-q", "-b", "master", "work")
    git(root, "config", "core.autocrlf", "false")
    put(root, ".gitignore", GITIGNORE.encode())
    put(root, ".env.example", b"RIGHTAPI_LLM_KEY=\n")
    put(root, "app/main.py", b"print('hi')\n")
    put(root, "workspace/.gitkeep", b"")
    git(root, "add", ".")
    git(root, "commit", "-qm", "init")

    put(root, ".env", f"RIGHTAPI_LLM_KEY={FAKE_KEY}\nDEMO_MODE=false\n".encode())
    put(root, "workspace/projects/p1/project.json", b"{}")
    put(root, "modal/h3_service.py", b"x = 1\r\ny = 2\r\n")
    put(root, "modal/__pycache__/h3_service.cpython-314.pyc", b"\x00junk")
    put(root, "config/skills_pro/ecommerce_planner/老庹_pro_skill.md", "---\nskill_id: pro\n---\n正文\n".encode())
    put(root, "tests/pro/test_pro.py", b"def test_x():\n    pass\n")
    put(root, "promo-film/build_promo_video.py", b"print('promo')\n")
    put(root, "promo-film/out/final.mp4", b"\x00big")
    put(root, "promo-film/.env.local", b"PROMO_TOKEN=abcdefgh12345\n")

    git(root, "remote", "add", "origin", str(tmp_path / "public.git"))
    git(root, "remote", "add", "private", str(tmp_path / "private.git"))
    return root


def snapshot(root: Path) -> tuple[str, str, str]:
    """当前分支、工作区状态、暂存区文件列表。"""
    return (
        git(root, "branch", "--show-current").stdout,
        git(root, "status", "--porcelain", "--ignored").stdout,
        git(root, "ls-files", "--stage").stdout,
    )


def test_path_problem():
    assert sync.path_problem(".env", allow_private=True) == "密钥文件"
    assert sync.path_problem("config/.env.production", allow_private=True) == "密钥文件"
    assert sync.path_problem(".env.example", allow_private=False) is None
    assert sync.path_problem("app/__pycache__/a.cpython-314.pyc", allow_private=True) == "缓存文件"
    assert sync.path_problem(".venv/Lib/site.py", allow_private=True) == "缓存文件"
    assert sync.path_problem("workspace/projects/p1/project.json", allow_private=True) == "运行产物"
    assert sync.path_problem("workspace/.gitkeep", allow_private=False) is None
    assert sync.path_problem("modal/h3_service.py", allow_private=False) == "私有目录"
    assert sync.path_problem("modal/h3_service.py", allow_private=True) is None
    assert sync.path_problem("app/modal/x.py", allow_private=False) is None


def test_backup_contains_private_dirs_and_leaves_worktree_alone(repo):
    pri = repo.parent / "private.git"
    before = snapshot(repo)

    sync.sync(repo, public=False)

    files = tree_files(pri, "main")
    assert {
        "app/main.py",
        "modal/h3_service.py",
        "config/skills_pro/ecommerce_planner/老庹_pro_skill.md",
        "tests/pro/test_pro.py",
        "promo-film/build_promo_video.py",
    } <= files
    assert not {f for f in files if "__pycache__" in f or f.startswith(("promo-film/out/", "workspace/projects"))}
    assert not {f for f in files if Path(f).name.startswith(".env") and f != ".env.example"}
    # 原样备份，保留文件自己的 CRLF（按字节读，text 模式会把 \r\n 转成 \n）
    blob = subprocess.run(["git", "cat-file", "-p", "main:modal/h3_service.py"], cwd=pri, capture_output=True)
    assert blob.stdout == b"x = 1\r\ny = 2\r\n"

    master = head(repo, "master")
    assert git(repo, "merge-base", "--is-ancestor", master, sync.BACKUP_REF, check=False).returncode == 0
    assert head(pri) == head(repo, sync.BACKUP_REF)
    assert head(repo.parent / "public.git") is None  # --private-only 不碰公开仓
    assert snapshot(repo) == before
    assert "laotuo-private-guard" in (repo / ".git/hooks/pre-push").read_text(encoding="utf-8")


def test_backup_is_incremental(repo):
    pri = repo.parent / "private.git"
    sync.sync(repo, public=False)
    first = head(pri)

    sync.sync(repo, public=False)
    assert head(pri) == first  # 没有变化不产生新提交

    put(repo, "modal/h3_service.py", b"x = 3\r\n")
    sync.sync(repo, public=False)
    second = head(pri)
    assert git(pri, "rev-list", "--parents", "-n1", second).stdout.split()[1:] == [first]

    put(repo, "app/main.py", b"print('v2')\n")
    git(repo, "commit", "-qam", "v2")
    sync.sync(repo, public=False)
    third = head(pri)
    assert git(pri, "rev-list", "--parents", "-n1", third).stdout.split()[1:] == [second, head(repo, "master")]
    assert git(pri, "cat-file", "-p", "main:app/main.py").stdout == "print('v2')\n"


def test_sync_pushes_public_without_private_dirs(repo):
    pub = repo.parent / "public.git"
    sync.sync(repo)

    assert head(pub) == head(repo, "master")
    files = tree_files(pub, "main")
    assert "app/main.py" in files
    assert not {f for f in files if f.split("/")[0] in {"modal", "promo-film", "tests", "config"}}


def test_pre_push_blocks_private_dirs_to_public(repo):
    sync.install_hook(repo)
    git(repo, "add", "-f", "modal/h3_service.py")
    git(repo, "commit", "-qm", "oops")

    proc = git(repo, "push", "origin", "master:main", check=False)
    assert proc.returncode != 0
    assert "modal/h3_service.py（私有目录）" in proc.stderr
    assert head(repo.parent / "public.git") is None

    with pytest.raises(sync.SyncError, match="不能公开"):
        sync.push_public(repo, head(repo, "master"))
    assert head(repo.parent / "public.git") is None


def test_pre_push_blocks_env_file_even_to_private(repo):
    sync.install_hook(repo)
    git(repo, "add", "-f", ".env")
    git(repo, "commit", "-qm", "oops")

    proc = git(repo, "push", "private", "master:main", check=False)
    assert proc.returncode != 0
    assert ".env（密钥文件）" in proc.stderr
    assert FAKE_KEY not in proc.stderr
    assert head(repo.parent / "private.git") is None


def test_secret_in_private_file_aborts_backup(repo):
    put(repo, "modal/config.py", f'API_KEY = "{FAKE_KEY}"\n'.encode())
    before = snapshot(repo)

    with pytest.raises(sync.SyncError, match="不能上传") as err:
        sync.sync(repo, public=False)

    assert "modal/config.py（疑似密钥）" in str(err.value)
    assert FAKE_KEY not in str(err.value)
    assert head(repo.parent / "private.git") is None
    assert head(repo, sync.BACKUP_REF) is None
    assert snapshot(repo) == before


def test_env_value_without_key_format_is_detected(repo):
    """.env 里的密钥不像常见格式时，也按原文比对出来。"""
    put(repo, ".env", b"MODAL_TOKEN_SECRET=plainsecret0042\n")
    put(repo, "tests/pro/conftest.py", b'TOKEN = "plainsecret0042"\n')

    with pytest.raises(sync.SyncError, match=r"tests/pro/conftest\.py（疑似密钥）"):
        sync.sync(repo, public=False)
