"""把开源代码推到 Hugging Face Space（Docker SDK），作为公共免 Key 在线演示。

Space 仓库和 GitHub 仓库的根目录要求不同：Space 根目录必须有 Dockerfile，README.md 必须带 Space 的 YAML 头。
所以不直接 git push master，而是另生成一个提交，只放运行需要的文件：

- master 最新提交里的 app/、config/、pyproject.toml、LICENSE（测试、文档、私有目录都不放）；
- deploy/hf_space/Dockerfile → Dockerfile；deploy/hf_space/Dockerfile.dockerignore → .dockerignore；
- deploy/hf_space/README.md → README.md（替换 GitHub 的 README）；
- Space 原有的 .gitattributes 保留。

用法（项目根目录，Windows Git Bash；先按 deploy/README_FREE_DEMO.md 登录 Hugging Face 并添加远端 hf）：
    git remote add hf https://huggingface.co/spaces/<用户名>/<Space 名>
    .venv/Scripts/python scripts/push_hf_space.py            # 生成提交并推送，Space 自动重新构建
    .venv/Scripts/python scripts/push_hf_space.py --dry-run  # 只检查、列出要推送的文件

做法与 scripts/sync_private_backup.py 相同：用临时索引生成提交，不切换分支、不碰工作区和暂存区；
新提交接在 Space 当前最新提交后面（不强推）。推送前检查密钥、缓存与私有目录，命中就中止。
只取 master 已提交的版本：本地还没提交的改动不会上线。
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sync_private_backup as sync

ROOT = sync.ROOT
REMOTE = "hf"
REMOTE_BRANCH = "main"
SOURCE_BRANCH = sync.PUBLIC_BRANCH
# master 里要上线的文件（目录以 / 结尾）
INCLUDE = ("app/", "config/", "pyproject.toml", "LICENSE")
# Space 根目录的文件 ← master 里的来源
PLACED = {
    "Dockerfile": "deploy/hf_space/Dockerfile",
    ".dockerignore": "deploy/hf_space/Dockerfile.dockerignore",
    "README.md": "deploy/hf_space/README.md",
}
KEEP_FROM_SPACE = (".gitattributes",)


def _entries(root: Path, treeish: str) -> dict[str, tuple[str, str]]:
    """文件树里每个文件：路径 -> (mode, blob sha)。"""
    out = sync._text(sync.git(root, "ls-tree", "-r", "-z", "--full-tree", treeish).stdout)
    entries = {}
    for item in out.split("\0"):
        if not item:
            continue
        meta, path = item.split("\t", 1)
        mode, kind, sha = meta.split()
        if kind == "blob":
            entries[path] = (mode, sha)
    return entries


def space_files(root: Path, master: str, remote_tip: str | None) -> dict[str, tuple[str, str]]:
    """要放进 Space 仓库的文件：路径 -> (mode, blob sha)。"""
    source = _entries(root, master)
    missing = [src for src in PLACED.values() if src not in source]
    if missing:
        raise sync.SyncError(f"{SOURCE_BRANCH} 里还没有这些部署文件，请先提交：{'、'.join(missing)}")
    files = {p: e for p, e in source.items() if any(p == i or (i.endswith("/") and p.startswith(i)) for i in INCLUDE)}
    for dst, src in PLACED.items():
        files[dst] = source[src]
    if remote_tip:
        current = _entries(root, remote_tip)
        for name in KEEP_FROM_SPACE:
            if name in current:
                files[name] = current[name]
    return dict(sorted(files.items()))


def build_tree(root: Path, files: dict[str, tuple[str, str]]) -> str:
    git_dir = Path(sync.out(root, "rev-parse", "--absolute-git-dir"))
    with tempfile.TemporaryDirectory(dir=git_dir, prefix="hf-space-") as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        sync.git(root, "read-tree", "--empty", env=env)
        info = "".join(f"{mode} blob {sha}\t{path}\0" for path, (mode, sha) in files.items())
        sync.git(root, "update-index", "-z", "--index-info", stdin=info.encode(), env=env)
        return sync.out(root, "write-tree", env=env)


def push(root: Path, *, dry_run: bool = False) -> str:
    master = sync.rev(root, SOURCE_BRANCH)
    if not master:
        raise sync.SyncError(f"找不到本地分支 {SOURCE_BRANCH}")
    url = sync.remote_url(root, REMOTE)
    if not url:
        raise sync.SyncError(f"还没有添加 Space 远端，先运行：git remote add {REMOTE} "
                             "https://huggingface.co/spaces/<用户名>/<Space 名>")
    print(f"连接 Space 仓库 {url} ……")
    sync.net(root, url, "fetch", "--quiet", REMOTE)
    remote_tip = sync.rev(root, f"refs/remotes/{REMOTE}/{REMOTE_BRANCH}")
    files = space_files(root, master, remote_tip)
    tree = build_tree(root, files)
    problems = sync.find_problems(root, [tree], allow_private=False)
    if problems:
        raise sync.SyncError("要推到 Space 的文件里有不能公开的内容，已中止，什么都没推送：\n"
                             + sync._bullets(problems))
    print(f"代码取自 {SOURCE_BRANCH} 最新提交 {master[:7]}，共 {len(files)} 个文件")
    if dry_run:
        return f"--dry-run：只检查，没有提交和推送。要推送的文件：\n{sync._bullets(files)}"
    if remote_tip and sync.out(root, "rev-parse", f"{remote_tip}^{{tree}}") == tree:
        return f"Space 已是最新（{remote_tip[:7]}），没有变化"
    message = f"deploy: 老庹 AI 电商内容工厂公共演示（{SOURCE_BRANCH} {master[:7]}）"
    args = ["-p", remote_tip] if remote_tip else []
    commit = sync.out(root, "commit-tree", tree, *args, "-m", message)
    sync.net(root, url, "push", "--quiet", REMOTE, f"{commit}:refs/heads/{REMOTE_BRANCH}")
    return f"已推送 {commit[:7]} 到 {REMOTE}/{REMOTE_BRANCH}，Space 会自动重新构建（首次约 5~10 分钟）"


def main(argv: list[str] | None = None) -> int:
    sync._utf8_console()
    parser = argparse.ArgumentParser(description="把开源代码推到 Hugging Face Space（公共免 Key 在线演示）")
    parser.add_argument("--dry-run", action="store_true", help="只检查并列出要推送的文件，不提交、不推送")
    args = parser.parse_args(argv)
    try:
        print(push(ROOT, dry_run=args.dry_run))
    except sync.SyncError as exc:
        print(f"\n推送中止：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
