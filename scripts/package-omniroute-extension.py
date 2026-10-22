#!/usr/bin/env python3
"""Build the source extension from canonical tracked files and the exact upstream tar.

No network, deployment, dependency install, or private runtime configuration is read.
The upstream Git tree is verified against its pinned root tree and every archive blob.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile

PIN = "edab7c3a59bf7d6f040d581dbda3a46adbee0d57"
UPSTREAM_TREE = "8e4e858856b64cde02b0a44212a94836f0ac210d"
VERSION = "3.8.52"
SOURCE_REMOVALS = "integrations/omniroute-extension/source-removals.json"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def git_hash(kind, data):
    return hashlib.sha1(f"{kind} {len(data)}\0".encode() + data).hexdigest()


def validate_relative(value):
    parts = PurePosixPath(value).parts
    if not value or "\\" in value or value.startswith("/") or any(
        not p or p in (".", "..") or ":" in p for p in value.split("/")
    ) or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"Unsafe source path: {value}")
    return parts


def verified_tree(document):
    if document.get("sha") not in (PIN, UPSTREAM_TREE) or document.get("truncated") is not False:
        raise ValueError("Complete exact-pinned upstream Git tree required")
    entries = document.get("tree")
    if not isinstance(entries, list):
        raise ValueError("Invalid Git tree inventory")
    by_path = {}
    children = {"": []}
    for entry in entries:
        name = entry["path"]
        validate_relative(name)
        if name in by_path or entry.get("type") not in ("tree", "blob"):
            raise ValueError(f"Duplicate or unsupported upstream entry: {name}")
        by_path[name] = entry
        parent, _, leaf = name.rpartition("/")
        children.setdefault(parent, []).append((leaf, entry))
        if entry["type"] == "tree":
            children.setdefault(name, [])
    for path, direct in children.items():
        raw = bytearray()
        # Git sorts a directory name as if it ends in '/'.
        for leaf, entry in sorted(direct, key=lambda row: (row[0] + ("/" if row[1]["type"] == "tree" else "")).encode()):
            mode = entry["mode"].lstrip("0")
            raw.extend(mode.encode() + b" " + leaf.encode() + b"\0" + bytes.fromhex(entry["sha"]))
        expected = UPSTREAM_TREE if path == "" else by_path[path]["sha"]
        if git_hash("tree", bytes(raw)) != expected:
            raise ValueError(f"Git tree hash mismatch: {path or '/'}")
    return {name: entry for name, entry in by_path.items() if entry["type"] == "blob"}


def read_archive(archive_path, upstream):
    prefix = f"OmniRoute-{PIN}/"
    content = {}
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive:
            if member.isdir():
                continue
            if not member.isfile() or not member.name.startswith(prefix):
                raise ValueError("Unexpected upstream archive member")
            relative = member.name[len(prefix):]
            validate_relative(relative)
            entry = upstream.get(relative)
            if not entry or relative in content or member.size > 32 * 1024 * 1024:
                raise ValueError(f"Unexpected/duplicate/oversized upstream blob: {relative}")
            if entry["mode"] not in ("100644", "100755"):
                raise ValueError(f"Unsupported upstream mode: {relative}")
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError(f"Unreadable upstream blob: {relative}")
            data = stream.read()
            if git_hash("blob", data) != entry["sha"]:
                raise ValueError(f"Upstream blob hash mismatch: {relative}")
            content[relative] = data
    if set(content) != set(upstream):
        raise ValueError("Upstream archive does not cover its complete verified tree")
    return content


def tracked_files(repo, upstream):
    raw = subprocess.run(["git", "ls-files", "--stage", "-z", "--", "omniroute/"], cwd=repo, check=True, capture_output=True).stdout
    entries = {}
    for row in raw.split(b"\0"):
        if not row:
            continue
        header, path_bytes = row.split(b"\t", 1)
        mode, _, stage = header.decode().split()
        if stage != "0" or mode not in ("100644", "100755"):
            raise ValueError("Only resolved regular tracked source files can be packaged")
        name = path_bytes.decode()[len("omniroute/"):]
        validate_relative(name)
        source = repo / "omniroute" / name
        if source.is_symlink() or any(parent.is_symlink() for parent in source.parents if parent != repo.parent):
            raise ValueError(f"Source symlinks cannot be packaged: {name}")
        if not source.exists() and name in upstream:
            continue
        entries[name] = (mode, source.read_bytes())
    return entries


def source_removals(repo, upstream, local):
    config = json.loads((repo / SOURCE_REMOVALS).read_text())
    if config.get("upstreamCommit") != PIN or not isinstance(config.get("paths"), list):
        raise ValueError("Exact-pinned explicit source-removals config required")
    seen = set()
    for name in config["paths"]:
        if not isinstance(name, str):
            raise ValueError("Removal path must be a string")
        validate_relative(name)
        folded = name.casefold()
        entry = upstream.get(name)
        if folded in seen or name == "package.json" or not entry or entry["mode"] != "100644":
            raise ValueError(f"Removal must be a unique regular official upstream path: {name}")
        source = repo / "omniroute" / name
        if name in local or source.exists() or source.is_symlink():
            raise ValueError(f"Explicit removal must be absent from canonical source: {name}")
        if any(parent.is_symlink() for parent in source.parents if parent != repo.parent):
            raise ValueError(f"Removal source ancestors cannot be symlinks: {name}")
        seen.add(folded)
    return sorted(config["paths"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream-archive", required=True, type=Path)
    parser.add_argument("--upstream-tree", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path, help="New empty package directory, outside canonical source")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parent.parent
    target = args.output.resolve()
    if target.is_relative_to(repo / "omniroute") or target.is_relative_to(repo / "integrations/omniroute-extension"):
        raise ValueError("Output must be outside canonical implementation and installer source")
    if target.exists():
        raise ValueError("Output must not exist; existing paths are never overwritten")
    upstream = verified_tree(json.loads(args.upstream_tree.read_text()))
    before = read_archive(args.upstream_archive, upstream)
    local = tracked_files(repo, upstream)
    removals = source_removals(repo, upstream, local)
    if json.loads(before["package.json"])["version"] != VERSION:
        raise ValueError("Upstream package version mismatch")
    if local.get("package.json", (None, None))[1] != before["package.json"]:
        raise ValueError("Canonical dependency/package metadata must match pinned upstream")
    files = []
    for name, (mode, after) in sorted(local.items()):
        original = before.get(name)
        if original == after:
            if name in upstream and upstream[name]["mode"] != mode:
                raise ValueError(f"Unexpected mode-only source delta: {name}")
            continue
        if name in upstream and upstream[name]["mode"] != mode:
            raise ValueError(f"Unexpected source mode delta: {name}")
        files.append({"path": name, "beforeSha256": digest(original) if original is not None else None,
                      "afterSha256": digest(after), "mode": int(mode, 8) & 0o777})
    for name in removals:
        files.append({"path": name, "beforeSha256": digest(before[name]), "afterSha256": None, "mode": 0o644})
    files.sort(key=lambda entry: entry["path"])
    # Only explicit reviewed removals are deletions; preserve every other missing vendor path.
    preserved = sorted(set(before) - set(local) - set(removals))
    package_source = repo / "integrations/omniroute-extension"
    package = json.loads((package_source / "package.json").read_text())
    manifest = {"schemaVersion": 2, "name": package["name"], "version": package["version"],
                "upstream": {"version": VERSION, "commit": PIN, "packageJsonSha256": digest(before["package.json"])},
                "files": files}
    target.mkdir(mode=0o755, parents=True)
    try:
        # Deliberate public package allowlist. No .env, DB, local captures, or node_modules.
        for name in ["package.json", "README.md", "LICENSE", "NOTICE"]:
            shutil.copyfile(package_source / name, target / name)
        for directory in ["bin", "lib", "test"]:
            if (package_source / directory).is_dir():
                shutil.copytree(package_source / directory, target / directory)
        for entry in files:
            name = entry["path"]
            if entry["afterSha256"] is not None:
                destination = target / "bundle/blobs" / entry["afterSha256"]
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(local[name][1])
            if entry["beforeSha256"] is not None:
                original_path = target / "bundle/blobs" / entry["beforeSha256"]
                original_path.parent.mkdir(parents=True, exist_ok=True)
                original_path.write_bytes(before[name])
        bundle = target / "bundle"
        bundle.mkdir(exist_ok=True)
        (bundle / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        (bundle / "provenance.json").write_text(json.dumps({"upstreamCommit": PIN, "upstreamGitTree": UPSTREAM_TREE, "allUpstreamBlobsVerified": len(upstream),
            "explicitlyRemovedUpstreamPaths": removals, "intentionallyPreservedUpstreamPaths": preserved}, indent=2) + "\n")
        for path in (target / "bin").glob("*.mjs"):
            path.chmod(0o755)
    except BaseException:
        # The directory was created above for this command only.
        shutil.rmtree(target)
        raise
    print(json.dumps({"package": package["name"], "version": package["version"], "files": len(files),
        "added": sum(entry["beforeSha256"] is None for entry in files),
        "modified": sum(entry["beforeSha256"] is not None and entry["afterSha256"] is not None for entry in files),
        "removed": len(removals), "removedUpstreamPaths": removals,
        "preservedUpstreamPaths": preserved, "output": str(target)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
