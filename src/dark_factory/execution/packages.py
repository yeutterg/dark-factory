"""Bounded, verified portable trees. No host paths, devices or escaping links."""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import posixpath
import tarfile
from pathlib import PurePosixPath

MAX_BYTES = 64 * 1024 * 1024


def identity(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def pack(paths):
    total = 0
    for source in paths.values():
        for item in [source] if source.is_file() else source.rglob("*"):
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
                if total > MAX_BYTES:
                    raise ValueError("expanded package exceeds limit")
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as tar:
        for name, source in paths.items():
            tar.inodes.clear()
            tar.add(source, arcname=name, recursive=True)
    data = output.getvalue()
    if len(data) > MAX_BYTES:
        raise ValueError("package exceeds transport limit")
    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        "data": base64.b64encode(data).decode(),
    }


def unpack(package, destination):
    if destination.exists():
        raise ValueError("package destination must be new")
    encoded = package["data"]
    if not isinstance(encoded, str) or len(encoded) > MAX_BYTES * 4 // 3 + 4:
        raise ValueError("package exceeds transport limit")
    data = base64.b64decode(encoded, validate=True)
    if hashlib.sha256(data).hexdigest() != package["sha256"]:
        raise ValueError("package digest mismatch")

    class LimitedReader:
        def __init__(self, source):
            self.source, self.remaining = source, MAX_BYTES * 2

        def read(self, size):
            result = self.source.read(min(size, self.remaining + 1))
            self.remaining -= len(result)
            if self.remaining < 0:
                raise ValueError("decompressed package exceeds limit")
            return result

    with (
        gzip.GzipFile(fileobj=io.BytesIO(data)) as compressed,
        tarfile.open(fileobj=LimitedReader(compressed), mode="r|") as tar,
    ):
        names = set()
        total = 0
        links = set()
        destination.mkdir(parents=True, mode=0o700)
        for item in tar:
            name = PurePosixPath(item.name)
            if (
                name.is_absolute()
                or ".." in name.parts
                or not name.parts
                or str(name) in names
                or not (item.isfile() or item.isdir() or item.issym())
            ):
                raise ValueError("unsafe package member")
            names.add(str(name))
            total += item.size
            if total > MAX_BYTES or len(names) > 100000:
                raise ValueError("expanded package exceeds limit")
            if item.issym():
                target = posixpath.normpath(
                    posixpath.join(str(name.parent), item.linkname)
                )
                if (
                    item.linkname.startswith("/")
                    or ".." in PurePosixPath(item.linkname).parts
                    or target == ".."
                    or target.startswith("../")
                ):
                    raise ValueError("escaping package link")
                links.add(str(name))
            if any(str(parent) in links for parent in PurePosixPath(name).parents):
                raise ValueError("package member descends through link")
            target = destination / item.name
            target.parent.mkdir(parents=True, exist_ok=True)
            if item.isdir():
                target.mkdir(exist_ok=True)
            elif item.issym():
                target.symlink_to(item.linkname)
            else:
                with tar.extractfile(item) as source, target.open("xb") as output:
                    while chunk := source.read(65536):
                        output.write(chunk)
                target.chmod(item.mode & 0o777 & ~0o022)
    return destination
