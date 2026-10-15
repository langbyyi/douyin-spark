"""Fetch and export verified upstream commits without changing the working tree."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

from dyspark.update_state import SHA, UpdateError

UPSTREAM = "https://github.com/langbyyi/douyin-spark.git"
BRANCH = "main"


def command(argv: list[str], *, cwd: Path, timeout: float = 90) -> str:
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise UpdateError(f"无法执行 {Path(argv[0]).name}，请检查安装和网络") from exc
    if result.returncode:
        # Do not expose subprocess output: it may include credential-bearing URLs.
        raise UpdateError(
            f"{Path(argv[0]).name} 执行失败（退出码 {result.returncode}），请查看部署环境"
        )
    return result.stdout.strip()


class GitSource:
    def __init__(self, root: Path, *, source_url: str | None = None) -> None:
        self.root = root.resolve()
        self.url = source_url or UPSTREAM
        self._test_source = source_url is not None

    def git(self, *args: str) -> str:
        return command(["git", *args], cwd=self.root)

    def current(self) -> str:
        return self.git("rev-parse", "HEAD")

    def clean(self) -> None:
        if not self._test_source:
            remote = self.git("remote", "get-url", "origin").removesuffix(".git").rstrip("/")
            if remote.lower() not in {
                "https://github.com/langbyyi/douyin-spark",
                "git@github.com:langbyyi/douyin-spark",
                "ssh://git@github.com/langbyyi/douyin-spark",
            }:
                raise UpdateError("一键更新仅支持官方仓库，请先核对 origin")
        if self.git("status", "--porcelain", "--untracked-files=normal"):
            raise UpdateError("源码目录存在本地改动，请先自行保存并处理后再更新")

    def check(self, current: str) -> dict:
        self.clean()
        if not isinstance(current, str) or not SHA.fullmatch(current):
            raise UpdateError("无法识别当前提交，不能自动更新")
        self.git("fetch", "--no-tags", "--", self.url, f"refs/heads/{BRANCH}")
        latest = self.git("rev-parse", "FETCH_HEAD^{commit}")
        if latest != current:
            self.validate(current, latest)
        return {
            "current": current,
            "latest": latest,
            "summary": self.git("log", "-1", "--format=%s", latest)[:300],
        }

    def validate(self, current: str, target: str) -> None:
        if not all(isinstance(sha, str) and SHA.fullmatch(sha) for sha in (current, target)):
            raise UpdateError("更新提交编号无效")
        if current == target:
            raise UpdateError("已经是该版本，无需更新")
        try:
            self.git("merge-base", "--is-ancestor", current, target)
        except UpdateError as exc:
            raise UpdateError("目标提交不是当前版本的后续版本，拒绝自动更新") from exc

    def stage(self, target: str, destination: Path) -> Path:
        if not isinstance(target, str) or not SHA.fullmatch(target):
            raise UpdateError("更新提交编号无效")
        destination.mkdir(mode=0o700, parents=True, exist_ok=False)
        with tempfile.TemporaryDirectory(prefix="dyspark-archive-") as temporary:
            archive = Path(temporary) / "source.tar"
            self.git("archive", "--format=tar", "-o", str(archive), target)
            with tarfile.open(archive) as stream:
                for member in stream:
                    path = destination / member.name
                    if not path.resolve().is_relative_to(destination.resolve()):
                        raise UpdateError("更新包包含越界路径")
                    if member.isdir():
                        path.mkdir(parents=True, exist_ok=True)
                    elif member.isfile():
                        path.parent.mkdir(parents=True, exist_ok=True)
                        with stream.extractfile(member) as source, path.open("xb") as output:
                            shutil.copyfileobj(source, output)
                        path.chmod(member.mode & 0o755)
                    else:
                        raise UpdateError("更新包包含不支持的链接或文件类型")
        package = destination / "src" / "dyspark"
        if not package.is_dir():
            raise UpdateError("更新包缺少 dyspark 程序")
        (package / "_build.json").write_text(json.dumps({"commit": target}), encoding="utf-8")
        return destination
