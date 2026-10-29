"""一键同步公开仓库与私人全量备份仓库。

公开仓库 origin 的 main 只放开源底座；私人仓库 private 的 main 在同一份代码之上，
再多备份公开仓 .gitignore 屏蔽的私有目录（PRIVATE_DIRS：modal/、config/skills_pro/、tests/pro/、promo-film/）
与私有文件（PRIVATE_FILES：config/categories/pet_herbal_otc.json）。

用法（项目根目录，Windows Git Bash）：
    .venv/Scripts/python scripts/sync_private_backup.py                 # 公开仓 + 私人全量备份
    .venv/Scripts/python scripts/sync_private_backup.py --private-only  # 只更新私人全量备份
    .venv/Scripts/python scripts/sync_private_backup.py --dry-run       # 只检查、列出要备份的私有文件

做法：
- 不切换分支，不碰工作区和暂存区。用临时索引把 master 最新提交的文件树加上磁盘上的私有目录，
  生成备份提交（父提交 = 上一次备份 + 当前 master），记在隐藏引用 refs/backup/private-full，
  再推到 private 的 main。隐藏引用不是分支：不会被误切换（切回 master 时 git 会删掉磁盘上的私有目录），
  也不会被 git push --all 带走。
- 公开代码只取已提交的版本：master 上没提交的改动不进备份，脚本会提示；私有目录按磁盘最新内容备份。
- 提交前检查，任何一项命中都中止、不推送：.env / .env.*（.env.example 除外）、.venv / __pycache__ /
  node_modules / workspace 等缓存产物、.env 与 ~/.modal.toml 中密钥的原文、常见 Key 格式。
- 安装 .git/hooks/pre-push：任何 git push（包括在 VS Code 里点推送）发出前都做同样的检查，
  并且私有目录与私有文件只允许推到 private 远端，推到公开仓 origin 一律拦截。
- 访问 github.com 时，本机代理 127.0.0.1:7890 能连上就走代理（环境变量 LAOTUO_GIT_PROXY 可改，写 off 关闭）。
"""

from __future__ import annotations

import argparse
import os
import re
import socket
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]

PUBLIC_REMOTE = "origin"
PUBLIC_BRANCH = "master"  # 本地开发分支，推到公开仓的 main
PUBLIC_REMOTE_BRANCH = "main"
PRIVATE_REMOTE = "private"
PRIVATE_REMOTE_BRANCH = "main"
BACKUP_REF = "refs/backup/private-full"

# 公开仓 .gitignore 屏蔽、只进私人仓的目录
PRIVATE_DIRS = {
    "modal": "自有 Modal GPU 部署脚本",
    "config/skills_pro": "商业 Pro 卡带包",
    "tests/pro": "依赖 Pro 卡带的测试",
    "promo-film": "宣传片生成脚本",
}
# 公开仓 .gitignore 屏蔽、只进私人仓的单个文件
PRIVATE_FILES = {
    "config/categories/pet_herbal_otc.json": "本机自用的类目配置",
}
# 私有目录里也不备份的子目录：宣传片渲染输出体积大，可以随时重新生成
SKIP_SUBDIRS = {"promo-film/out"}
CACHE_DIRS = {"__pycache__", ".venv", "venv", "node_modules", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
CACHE_SUFFIXES = {".pyc", ".pyo"}
JUNK_FILES = {".DS_Store", "Thumbs.db", "desktop.ini"}
MAX_FILE_BYTES = 50 * 1024 * 1024  # GitHub 单文件超过 50MB 警告、超过 100MB 拒收

# 常见 Key 格式（git grep -E）；Modal 令牌以 ak- / as- 开头
SECRET_PATTERNS = (
    r"(^|[^A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}",
    r"(^|[^A-Za-z0-9])a[ks]-[A-Za-z0-9]{20,}",
    r"gh[pousr]_[A-Za-z0-9]{30,}",
    r"github_pat_[A-Za-z0-9_]{30,}",
    r"hf_[A-Za-z0-9]{30,}",
    r"AKIA[0-9A-Z]{16}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
)
SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD", re.IGNORECASE)
MODAL_TOML = Path.home() / ".modal.toml"

DEFAULT_PROXY = "http://127.0.0.1:7890"
HOOK_MARK = "laotuo-private-guard"
HOOK = """#!/bin/sh
# {mark}：由 scripts/sync_private_backup.py 安装，重新运行该脚本会自动更新，请勿手改。
# 每次 git push 前检查要推送的提交：不能含 .env / 密钥 / 缓存；私有目录（modal/ 等）只能推到 private 远端。
py="{python}"
script="{script}"
if [ ! -f "$py" ] || [ ! -f "$script" ]; then
  echo "[pre-push] 找不到检查脚本或 Python，已拦截推送。" >&2
  echo "[pre-push] 请运行：python scripts/sync_private_backup.py --install-hook" >&2
  exit 1
fi
exec "$py" "$script" --pre-push "$@"
"""


class SyncError(RuntimeError):
    """同步中止；消息直接给用户看。"""


def _text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def git(
    root: Path,
    *args: str,
    stdin: bytes | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
    timeout: float = 120,
) -> subprocess.CompletedProcess[bytes]:
    try:
        proc = subprocess.run(
            ["git", "-c", "core.quotepath=off", *args],
            cwd=root,
            input=stdin,
            capture_output=True,
            timeout=timeout,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})},
        )
    except subprocess.TimeoutExpired as exc:
        raise SyncError(f"git {args[0]} 超时（{timeout:.0f} 秒）") from exc
    if check and proc.returncode != 0:
        raise SyncError(f"git {args[0]} 失败：{_text(proc.stderr).strip()}")
    return proc


def out(root: Path, *args: str, **kwargs) -> str:
    return _text(git(root, *args, **kwargs).stdout).strip()


def rev(root: Path, name: str) -> str | None:
    proc = git(root, "rev-parse", "--verify", "--quiet", f"{name}^{{commit}}", check=False)
    return _text(proc.stdout).strip() if proc.returncode == 0 else None


def is_ancestor(root: Path, older: str, newer: str) -> bool:
    return git(root, "merge-base", "--is-ancestor", older, newer, check=False).returncode == 0


def remote_url(root: Path, name: str) -> str | None:
    proc = git(root, "remote", "get-url", name, check=False)
    return _text(proc.stdout).strip() if proc.returncode == 0 else None


def _same_url(a: str, b: str) -> bool:
    def norm(url: str) -> str:
        return url.strip().rstrip("/").removesuffix(".git").lower()

    return norm(a) == norm(b)


def _is_zero(sha: str) -> bool:
    return set(sha) == {"0"}


# ---------------------------------------------------------------- 联网


def proxy_env(url: str) -> dict[str, str]:
    """github.com 直连常被重置：本机代理端口能连上，就让这条 git 命令走代理（不改 git 配置）。"""
    if not url.startswith(("http://", "https://")):
        return {}
    proxy = os.environ.get("LAOTUO_GIT_PROXY", DEFAULT_PROXY).strip()
    if not proxy or proxy.lower() == "off":
        return {}
    parsed = urlparse(proxy)
    try:
        with socket.create_connection((parsed.hostname or "127.0.0.1", parsed.port or 80), timeout=0.5):
            pass
    except OSError:
        return {}
    return {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.proxy", "GIT_CONFIG_VALUE_0": proxy}


def net(root: Path, url: str, *args: str) -> subprocess.CompletedProcess[bytes]:
    """联网的 git 命令；网络偶尔断开，失败时自动重试一次。"""
    env = proxy_env(url)
    proc = git(root, *args, env=env, check=False, timeout=600)
    if proc.returncode != 0:
        proc = git(root, *args, env=env, check=False, timeout=600)
    if proc.returncode != 0:
        raise SyncError(f"git {args[0]} 失败：\n{_text(proc.stderr).strip()}")
    return proc


# ---------------------------------------------------------------- 检查


def is_env_file(name: str) -> bool:
    return name == ".env" or (name.startswith(".env.") and name != ".env.example")


def path_problem(path: str, *, allow_private: bool) -> str | None:
    """仓库里这个路径不能上传的原因；可以上传返回 None。"""
    parts = path.split("/")
    if is_env_file(parts[-1]):
        return "密钥文件"
    if any(p in CACHE_DIRS for p in parts[:-1]) or Path(parts[-1]).suffix in CACHE_SUFFIXES:
        return "缓存文件"
    if parts[0] == "workspace" and path != "workspace/.gitkeep":
        return "运行产物"
    if not allow_private and any(path.startswith(d + "/") for d in PRIVATE_DIRS):
        return "私有目录"
    if not allow_private and path in PRIVATE_FILES:
        return "私有文件"
    return None


def secret_values(root: Path) -> list[str]:
    """本机真实密钥原文（.env 里名字含 KEY / TOKEN / SECRET / PASSWORD 的值，以及 Modal 令牌），只用于比对。"""
    values: list[str] = []
    env_file = root / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
            if m and SECRET_NAME.search(m[1]):
                values.append(m[2].strip().strip("'\""))
    if MODAL_TOML.is_file():
        try:
            profiles = tomllib.loads(MODAL_TOML.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            profiles = {}
        for profile in profiles.values():
            if isinstance(profile, dict):
                values += [str(profile.get(k, "")) for k in ("token_id", "token_secret")]
    return sorted({v for v in values if len(v) >= 8})


def _grep_hits(proc: subprocess.CompletedProcess[bytes]) -> set[str]:
    if proc.returncode > 1:  # 0 = 有命中，1 = 没有命中
        raise SyncError(f"git grep 失败：{_text(proc.stderr).strip()}")
    return {line.split(":", 1)[1] for line in _text(proc.stdout).splitlines() if ":" in line}


def find_problems(root: Path, objects: list[str], *, allow_private: bool) -> list[str]:
    """检查若干提交 / 文件树里不能上传的文件。只列文件名与原因，绝不回显密钥。"""
    problems: dict[str, str] = {}
    for obj in objects:
        for path in _text(git(root, "ls-tree", "-r", "-z", "--name-only", obj).stdout).split("\0"):
            if path and path not in problems and (reason := path_problem(path, allow_private=allow_private)):
                problems[path] = reason
    values = "".join(v + "\n" for v in secret_values(root)).encode()
    patterns = [arg for p in SECRET_PATTERNS for arg in ("-e", p)]
    for i in range(0, len(objects), 100):  # 分批，避免 Windows 命令行过长
        chunk = objects[i : i + 100]
        hits = _grep_hits(git(root, "grep", "-l", "-I", "-E", *patterns, *chunk, "--", check=False))
        if values:
            hits |= _grep_hits(git(root, "grep", "-l", "-I", "-F", "-f", "-", *chunk, "--", stdin=values, check=False))
        for path in hits:
            problems.setdefault(path, "疑似密钥")
    return [f"{p}（{r}）" for p, r in sorted(problems.items())]


def _bullets(items: Iterable[str]) -> str:
    return "\n".join(f"  - {item}" for item in items)


# ---------------------------------------------------------------- 备份内容


def collect_private_files(root: Path) -> tuple[list[str], list[str]]:
    """磁盘上要备份的私有文件（仓库相对路径），以及跳过的文件（附原因）。"""
    files: list[str] = []
    skipped: list[str] = []
    for top in PRIVATE_DIRS:
        for dirpath, dirnames, filenames in os.walk(root / top):
            rel = Path(dirpath).relative_to(root).as_posix()
            dirnames[:] = sorted(
                d for d in dirnames if d not in CACHE_DIRS and d != ".git" and f"{rel}/{d}" not in SKIP_SUBDIRS
            )
            for name in sorted(filenames):
                path, full = f"{rel}/{name}", Path(dirpath, name)
                if name in JUNK_FILES or full.suffix in CACHE_SUFFIXES or full.is_symlink():
                    continue
                if is_env_file(name):
                    skipped.append(f"{path}（密钥文件）")
                elif full.stat().st_size > MAX_FILE_BYTES:
                    skipped.append(f"{path}（超过 50MB）")
                else:
                    files.append(path)
    for path in PRIVATE_FILES:
        full = root / path
        if not full.is_file() or full.is_symlink():
            continue
        if full.stat().st_size > MAX_FILE_BYTES:
            skipped.append(f"{path}（超过 50MB）")
        else:
            files.append(path)
    return files, skipped


def build_tree(root: Path, base: str, files: list[str]) -> str:
    """base 提交的文件树 + 磁盘上的私有文件 → 新文件树。用临时索引，不碰真正的暂存区。"""
    git_dir = Path(out(root, "rev-parse", "--absolute-git-dir"))
    with tempfile.TemporaryDirectory(dir=git_dir, prefix="private-backup-") as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        git(root, "read-tree", base, env=env)
        if files:
            # --no-filters：按磁盘字节原样入库，保留各文件自己的 CRLF / LF
            listing = "".join(f + "\n" for f in files).encode()
            blobs = out(root, "hash-object", "-w", "--no-filters", "--stdin-paths", stdin=listing).split()
            info = "".join(f"100644 blob {sha}\t{path}\0" for sha, path in zip(blobs, files, strict=True))
            git(root, "update-index", "-z", "--index-info", stdin=info.encode(), env=env)
        return out(root, "write-tree", env=env)


def private_counts(files: list[str]) -> list[str]:
    counts = {d: sum(f.startswith(d + "/") for f in files) for d in PRIVATE_DIRS}
    lines = [f"{d}/：{n} 个文件（{PRIVATE_DIRS[d]}）" for d, n in counts.items() if n]
    return lines + [f"{f}（{PRIVATE_FILES[f]}）" for f in PRIVATE_FILES if f in files]


def backup_message(master: str, files: list[str]) -> str:
    lines = [
        f"backup: 私人全量备份 {datetime.now():%Y-%m-%d %H:%M}（公开代码 {PUBLIC_BRANCH} {master[:7]}）",
        "",
        "在公开代码之上多备份的私有目录与文件：",
    ]
    lines += [f"- {c}" for c in private_counts(files)] or ["- （无）"]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 钩子


def install_hook(root: Path) -> str:
    hooks = Path(out(root, "rev-parse", "--git-path", "hooks"))
    hook = (hooks if hooks.is_absolute() else root / hooks) / "pre-push"
    python, script = Path(sys.executable).as_posix(), Path(__file__).resolve().as_posix()
    body = HOOK.format(mark=HOOK_MARK, python=python, script=script)
    if hook.exists():
        current = hook.read_text(encoding="utf-8", errors="replace")
        if HOOK_MARK not in current:
            return f"{hook} 已有别的钩子，没有覆盖（本脚本推送前仍会自检）"
        if current == body:
            return "已启用"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_bytes(body.encode("utf-8"))
    hook.chmod(0o755)
    return "已安装"


def _new_commits(root: Path, tip: str, remote: str, exclude: Iterable[str] = ()) -> list[str]:
    """tip 中远端 remote 还没有的提交。"""
    return out(root, "rev-list", tip, *(f"^{e}" for e in exclude), "--not", f"--remotes={remote}").split()


def pre_push(root: Path, remote: str, url: str, lines: Iterable[str]) -> int:
    """git pre-push 钩子：返回非 0 时 git 取消整次推送。"""
    private_url = remote_url(root, PRIVATE_REMOTE)
    allow_private = bool(private_url) and _same_url(url, private_url)
    commits: list[str] = []
    for line in lines:
        parts = line.split()
        if len(parts) != 4 or _is_zero(parts[1]):  # 删除远端分支不用检查
            continue
        remote_sha = parts[3]
        known = [remote_sha] if not _is_zero(remote_sha) and rev(root, remote_sha) else []
        commits += _new_commits(root, parts[1], remote, known)
    problems = find_problems(root, list(dict.fromkeys(commits)), allow_private=allow_private)
    if not problems:
        return 0
    where = f"私人仓库 {remote}" if allow_private else f"{remote}（它不是私人仓库 {PRIVATE_REMOTE}）"
    print(f"\n[pre-push] 已拦截推送到 {where}，以下文件不能上传：\n{_bullets(problems)}", file=sys.stderr)
    if not allow_private and any(p.endswith(("（私有目录）", "（私有文件）")) for p in problems):
        print("[pre-push] 私有目录与私有文件只能用 python scripts/sync_private_backup.py 备份到私人仓库。",
              file=sys.stderr)
    return 1


# ---------------------------------------------------------------- 同步


def _warn_local_state(root: Path) -> None:
    dirty = [line for line in _text(git(root, "status", "--porcelain").stdout).splitlines() if line]
    if dirty:
        print(f"提示：有 {len(dirty)} 个文件改动还没提交，这次的公开代码部分只包含已提交的版本。")
    paths = [*(f"{d}/x" for d in PRIVATE_DIRS), *PRIVATE_FILES]
    probe = [p.removesuffix("x") if p.endswith("/x") else p for p in paths
             if git(root, "check-ignore", "-q", "--no-index", p, check=False).returncode]
    if probe:
        print(f"警告：.gitignore 没有屏蔽 {'、'.join(probe)}，git add 时可能被误加进公开代码"
              "（推送时 pre-push 检查仍会拦截）。")


def _previous_backup(root: Path, remote_tip: str | None) -> str | None:
    local = rev(root, BACKUP_REF)
    if not remote_tip or remote_tip == local:
        return local
    if not local or is_ancestor(root, local, remote_tip):
        return remote_tip  # 本机没有记录，或别的电脑备份过，接在私人仓库最新提交后面
    if is_ancestor(root, remote_tip, local):
        return local  # 上次提交了但没推上去
    raise SyncError(
        f"私人仓库的 {PRIVATE_REMOTE_BRANCH} 和本机的备份记录分叉了（可能在别处改过私人仓库），请人工处理。"
    )


def backup_to_private(root: Path, master: str, tree: str, files: list[str]) -> str:
    url = remote_url(root, PRIVATE_REMOTE)
    if not url:
        raise SyncError(f"还没有绑定私人仓库，先运行：git remote add {PRIVATE_REMOTE} <私人仓库地址>")
    print(f"连接私人仓库 {url} ……")
    net(root, url, "fetch", "--quiet", PRIVATE_REMOTE)
    remote_tip = rev(root, f"refs/remotes/{PRIVATE_REMOTE}/{PRIVATE_REMOTE_BRANCH}")
    local = rev(root, BACKUP_REF)
    prev = _previous_backup(root, remote_tip)
    master_included = bool(prev) and is_ancestor(root, master, prev)
    if master_included and out(root, "rev-parse", f"{prev}^{{tree}}") == tree:
        backup = prev
    else:
        # 父提交：上一次备份 + 当前 master（master 已在上次备份里时不重复挂）
        parents = [p for p in (prev, None if master_included else master) if p]
        args = [arg for p in parents for arg in ("-p", p)]
        backup = out(root, "commit-tree", tree, *args, "-F", "-", stdin=backup_message(master, files).encode())
    if backup != local:
        git(root, "update-ref", "-m", "private backup", BACKUP_REF, backup, local or "0" * 40)
    if backup == remote_tip:
        return f"已是最新（{backup[:7]}），没有变化"
    problems = find_problems(root, _new_commits(root, backup, PRIVATE_REMOTE), allow_private=True)
    if problems:
        raise SyncError(f"要推到私人仓库的提交里有不能上传的文件：\n{_bullets(problems)}")
    net(root, url, "push", "--quiet", PRIVATE_REMOTE, f"{BACKUP_REF}:refs/heads/{PRIVATE_REMOTE_BRANCH}")
    return f"已推送 {backup[:7]}"


def push_public(root: Path, master: str) -> str:
    url = remote_url(root, PUBLIC_REMOTE)
    if not url:
        return f"没有远端 {PUBLIC_REMOTE}，跳过"
    print(f"连接公开仓库 {url} ……")
    net(root, url, "fetch", "--quiet", PUBLIC_REMOTE)
    remote_tip = rev(root, f"refs/remotes/{PUBLIC_REMOTE}/{PUBLIC_REMOTE_BRANCH}")
    if remote_tip == master:
        return f"已是最新（{master[:7]}）"
    if remote_tip and not is_ancestor(root, remote_tip, master):
        return f"公开仓库有本机 {PUBLIC_BRANCH} 没有的提交，为避免覆盖，这次没有推送（需要人工合并）"
    commits = _new_commits(root, master, PUBLIC_REMOTE)
    problems = find_problems(root, commits, allow_private=False)
    if problems:
        raise SyncError(f"要推到公开仓库的提交里有不能公开的文件，已中止：\n{_bullets(problems)}")
    print(out(root, "log", "--oneline", "--no-decorate", master, "--not", f"--remotes={PUBLIC_REMOTE}"))
    net(root, url, "push", "--quiet", PUBLIC_REMOTE, f"{master}:refs/heads/{PUBLIC_REMOTE_BRANCH}")
    return f"已推送 {len(commits)} 个提交（{master[:7]}）"


def sync(root: Path, *, public: bool = True, dry_run: bool = False) -> None:
    master = rev(root, PUBLIC_BRANCH)
    if not master:
        raise SyncError(f"找不到本地分支 {PUBLIC_BRANCH}")
    current = out(root, "branch", "--show-current") or "（不在任何分支上）"
    print(f"当前分支：{current}；公开代码取自 {PUBLIC_BRANCH} 最新提交 {master[:7]}")
    _warn_local_state(root)
    print(f"pre-push 防泄露检查：{install_hook(root)}")

    files, skipped = collect_private_files(root)
    tree = build_tree(root, master, files)
    problems = find_problems(root, [tree], allow_private=True)
    if problems:
        raise SyncError(f"备份内容里有不能上传的文件，已中止，什么都没推送：\n{_bullets(problems)}")
    print(f"比公开仓多备份：\n{_bullets(private_counts(files) or ['（磁盘上没有私有目录和私有文件）'])}")
    if skipped:
        print(f"跳过：\n{_bullets(skipped)}")
    if dry_run:
        print(f"要备份的私有文件：\n{_bullets(files)}\n（--dry-run：只检查，没有提交和推送）")
        return

    results = [f"私人仓库 {PRIVATE_REMOTE}/{PRIVATE_REMOTE_BRANCH}：{backup_to_private(root, master, tree, files)}"]
    if public:
        results.append(f"公开仓库 {PUBLIC_REMOTE}/{PUBLIC_REMOTE_BRANCH}：{push_public(root, master)}")
    results.append(f"当前分支仍是 {current}，工作区和暂存区没有改动")
    print(f"\n完成：\n{_bullets(results)}")


def _utf8_console() -> None:
    """管道或 Git Bash 里 Python 默认按 GBK 输出，中文会乱码；不是控制台时改成 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        if not stream.isatty() and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    parser = argparse.ArgumentParser(description="一键同步：公开仓库 origin + 私人全量备份仓库 private")
    parser.add_argument("--private-only", action="store_true", help="只更新私人全量备份，不推送公开仓库")
    parser.add_argument("--dry-run", action="store_true", help="只检查并列出要备份的私有文件，不提交、不推送")
    parser.add_argument("--install-hook", action="store_true", help="只安装 / 更新 pre-push 防泄露检查")
    parser.add_argument("--pre-push", nargs=2, metavar=("REMOTE", "URL"), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.pre_push:
            root = Path(out(Path.cwd(), "rev-parse", "--show-toplevel"))
            return pre_push(root, *args.pre_push, sys.stdin.read().splitlines())
        if args.install_hook:
            print(f"pre-push 防泄露检查：{install_hook(ROOT)}")
            return 0
        sync(ROOT, public=not args.private_only, dry_run=args.dry_run)
    except SyncError as exc:
        print(f"\n同步中止：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
