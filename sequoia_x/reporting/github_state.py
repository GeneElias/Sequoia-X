"""Authenticated encryption for Actions state, stored on a dedicated Git branch.

Only explicit ledger/report paths are archived. No .env, key files, cache or
plaintext private data is ever written to the remote branch. Missing state and
wrong keys fail closed rather than resetting the portfolio.
"""

from contextlib import closing
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import subprocess
import tarfile
import tempfile

from cryptography.fernet import Fernet, InvalidToken


BRANCH = "sequoia-state"
LIMIT = 20 * 1024 * 1024


class StateError(RuntimeError):
    pass


def archive_paths(root: Path, valuations: Path | None = None) -> dict[str, Path]:
    paths = {}
    for name in ("paper.sqlite", "delivery.sqlite", "diagnostics.json"):
        p = root / "_state" / name
        if p.exists():
            paths["state/" + name] = p
    # Current market observations + full accounting history in the database.
    for market in ("A", "US"):
        directories = sorted((root / "paper" / "daily").glob(f"*-{market}"))
        if directories:
            for name in ("report.json", "paper.json", "cards.json", "report.md"):
                p = directories[-1] / name
                if p.exists():
                    paths[f"daily/{directories[-1].name}/{name}"] = p
    for directory in sorted((root / "paper" / "weekly").glob("*"))[-52:]:
        for name in ("review.json", "cards.json", "review.md"):
            p = directory / name
            if p.exists():
                paths[f"weekly/{directory.name}/{name}"] = p
    if valuations and valuations.is_file():
        paths["state/valuations.json"] = valuations
    return paths


def encrypted_snapshot(root: Path, key: bytes, valuations: Path | None = None) -> bytes:
    if not (root / "_state" / "paper.sqlite").exists():
        raise StateError("账本缺失，拒绝保存空白状态")
    buffer = io.BytesIO()
    with tempfile.TemporaryDirectory(prefix="sequoia-state-backup-") as directory:
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for name, path in archive_paths(root, valuations).items():
                if path.is_symlink():
                    raise StateError("拒绝打包符号链接")
                content = path.read_bytes()
                if name.endswith(".sqlite"):
                    backup = Path(directory) / path.name
                    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as source:
                        with closing(sqlite3.connect(backup)) as destination:
                            source.backup(destination)
                    content = backup.read_bytes()
                if len(content) > LIMIT:
                    raise StateError("状态文件超过大小上限")
                info = tarfile.TarInfo(name)
                info.size = len(content)
                info.mode = 0o600
                archive.addfile(info, io.BytesIO(content))
    if len(buffer.getvalue()) > LIMIT:
        raise StateError("加密前归档超过大小上限")
    return Fernet(key).encrypt(buffer.getvalue())


def restore_snapshot(token: bytes, root: Path, key: bytes) -> None:
    if len(token) > LIMIT * 2:
        raise StateError("远端状态超过大小上限")
    try:
        raw = Fernet(key).decrypt(token)
    except (InvalidToken, ValueError):
        raise StateError("无法解密状态；请检查密钥，不得重置账本绕过") from None
    planned = []
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        total = 0
        for member in archive.getmembers():
            parts = PurePosixPath(member.name).parts
            if not member.isfile() or ".." in parts or not parts or PurePosixPath(member.name).is_absolute():
                raise StateError("状态归档包含非法路径")
            if parts[0] == "state" and len(parts) == 2 and parts[1] in {"paper.sqlite", "delivery.sqlite", "valuations.json", "diagnostics.json"}:
                target = root / "_state" / parts[1]
            elif (parts[0] in {"daily", "weekly"} and len(parts) == 3
                  and parts[2] in {"report.json", "paper.json", "cards.json", "report.md", "review.json", "review.md"}):
                target = root / "paper" / Path(*parts)
            else:
                raise StateError("归档文件不在白名单")
            total += member.size
            if total > LIMIT or member.size < 0:
                raise StateError("解压后超过大小上限")
            if target.is_symlink() or not target.resolve().is_relative_to(root.resolve()):
                raise StateError("状态目标越出本地目录")
            planned.append((target, archive.extractfile(member).read()))
    if not any(path.name == "paper.sqlite" for path, _ in planned):
        raise StateError("加密状态中没有账本")
    for target, content in planned:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        target.chmod(0o600)
    for name in ("paper.sqlite", "delivery.sqlite"):
        path = root / "_state" / name
        if path.exists():
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
                if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise StateError("恢复后的SQLite完整性检查失败")


class GitHubState:
    def __init__(self, repository: Path, root: Path, key: bytes):
        self.repository, self.root, self.key = repository, root, key
        Fernet(key)  # validate before doing network work
        self.tip = None
        self.restored = False

    def git(self, *args, content: bytes | None = None) -> bytes:
        result = subprocess.run(["git", *args], cwd=self.repository, input=content, capture_output=True)
        if result.returncode:
            raise StateError("Git状态操作失败；原始输出已隐藏，避免泄露认证信息")
        return result.stdout

    def remote_tip(self) -> str | None:
        output = self.git("ls-remote", "origin", "refs/heads/" + BRANCH).decode().strip()
        return output.split()[0] if output else None

    def restore(self):
        self.tip = self.remote_tip()
        if not self.tip:
            raise StateError("远端账本分支不存在；需要显式初始化，不能自动清空")
        self.git("fetch", "--no-tags", "origin", "refs/heads/" + BRANCH)
        restore_snapshot(self.git("show", self.tip + ":state.fernet"), self.root, self.key)
        self.restored = True

    def save(self, initialise: bool = False, valuations: Path | None = None):
        remote = self.remote_tip()
        if initialise:
            if remote:
                raise StateError("远端已有账本，拒绝重新初始化覆盖")
        elif not self.restored or remote != self.tip:
            raise StateError("状态未恢复或远端已改变，拒绝覆盖")
        valuations = valuations or self.root / "_state" / "valuations.json"
        encrypted = encrypted_snapshot(self.root, self.key, valuations)
        blob = self.git("hash-object", "-w", "--stdin", content=encrypted).decode().strip()
        description = ("# Encrypted Sequoia-X simulation state\n\n"
                       "Authenticated encrypted ledger and private report snapshots.\n"
                       "Requires SEQUOIA_STATE_KEY from repository Actions Secrets.\n"
                       "Do not reset this branch or upload plaintext state/credentials.\n")
        readme = self.git("hash-object", "-w", "--stdin", content=description.encode()).decode().strip()
        tree = self.git("mktree", content=f"100644 blob {readme}\tREADME.md\n100644 blob {blob}\tstate.fernet\n".encode()).decode().strip()
        args = ["-c", "user.name=Sequoia-X automation", "-c", "user.email=sequoia-x@users.noreply.github.com",
                "commit-tree", tree]
        if self.tip:
            args += ["-p", self.tip]
        commit = self.git(*args, "-m", "Persist encrypted paper state " + datetime.now(timezone.utc).isoformat()).decode().strip()
        # Fast-forward only. Never force-push or overwrite a concurrent writer.
        self.git("push", "origin", commit + ":refs/heads/" + BRANCH)
        self.tip, self.restored = commit, True
