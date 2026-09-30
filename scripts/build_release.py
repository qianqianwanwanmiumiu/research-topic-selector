#!/usr/bin/env python3
"""Build and verify the universal and WorkBuddy Skill release archives."""

import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import zipfile


ROOT = Path(__file__).resolve().parents[1]
NAME = "research-topic-selector"
FILES = (
    "SKILL.md",
    "LICENSE",
    "agents/openai.yaml",
    "scripts/search_literature.py",
    "references/database-search.md",
    "references/example.md",
    "references/sources.md",
    "references/worksheet.md",
)
DESCRIPTION_EN = (
    "Compare research topics, examine key assumptions, and decide whether to "
    "continue, pivot, or stop using evidence status and minimal validation plans."
)


def frontmatter(data):
    match = re.match(rb"\A---(?P<newline>\r?\n)(?P<header>.*?)^---[ \t]*\r?\n(?P<body>.*)\Z",
                     data, re.MULTILINE | re.DOTALL)
    if not match:
        raise ValueError("SKILL.md must start with a standard YAML frontmatter block.")
    return match["header"], match["body"], match["newline"]


def scalar(header, name, parent=None):
    """Read the canonical file's one-line YAML scalars without a YAML dependency."""
    lines = header.decode("utf-8").splitlines()
    if parent:
        try:
            start = lines.index(parent + ":") + 1
        except ValueError:
            raise ValueError("Missing frontmatter block: " + parent) from None
        lines = lines[start:]
        end = next((index for index, line in enumerate(lines) if line and not line[0].isspace()), len(lines))
        lines = lines[:end]
    prefix = "  " if parent else ""
    values = [line[len(prefix + name + ":"):].strip() for line in lines if line.startswith(prefix + name + ":")]
    if len(values) != 1 or not values[0]:
        raise ValueError("Expected one nonempty frontmatter scalar: " + (parent + "." if parent else "") + name)
    value = values[0]
    if value.startswith('"'):
        value = json.loads(value)
    elif value.startswith("'") and value.endswith("'"):
        value = value[1:-1].replace("''", "'")
    elif value[0] in "|>[{&*!'":
        raise ValueError("Use a plain or quoted one-line YAML scalar for " + name)
    if not isinstance(value, str) or not value:
        raise ValueError("Frontmatter value must be a nonempty string: " + name)
    return value


def workbuddy_skill(data, version, author):
    header, body, newline = frontmatter(data)
    fields = {"description_zh": scalar(header, "description"), "description_en": DESCRIPTION_EN,
              "version": version, "author": author}
    for name in fields:
        if re.search(rb"^" + name.encode("ascii") + rb":", header, re.MULTILINE):
            raise ValueError("Canonical frontmatter already has WorkBuddy field: " + name)
    additions = newline.join((name + ": " + json.dumps(value, ensure_ascii=False)).encode("utf-8")
                             for name, value in fields.items()) + newline
    return b"---" + newline + header + additions + b"---" + newline + body


def archive_bytes(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, data in files.items():
            entry = zipfile.ZipInfo(NAME + "/" + path, date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, data, compresslevel=9)
    return buffer.getvalue()


def verify_archive(data, expected):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        if len(names) != len(expected) or set(names) != {NAME + "/" + path for path in expected}:
            raise ValueError("ZIP entry whitelist check failed.")
        for path, original in expected.items():
            if archive.read(NAME + "/" + path) != original:
                raise ValueError("ZIP byte parity check failed: " + path)


def build(out_dir):
    source = ROOT / "skills" / NAME
    files = {path: (source / path).read_bytes() for path in FILES}
    header, body, _ = frontmatter(files["SKILL.md"])
    if scalar(header, "name") != NAME:
        raise ValueError("Canonical skill name does not match the release directory.")
    version = scalar(header, "version", "metadata")
    author = scalar(header, "author", "metadata")
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?", version):
        raise ValueError("metadata.version must be a semantic version, such as 0.3.0.")
    workbuddy = {path: data for path, data in files.items() if path != "agents/openai.yaml"}
    workbuddy["SKILL.md"] = workbuddy_skill(files["SKILL.md"], version, author)
    wb_header, wb_body, _ = frontmatter(workbuddy["SKILL.md"])
    if wb_body != body or any(workbuddy[path] != files[path] for path in workbuddy if path != "SKILL.md"):
        raise ValueError("WorkBuddy shared-body or file parity check failed.")
    for name, expected in (("description_zh", scalar(header, "description")),
                           ("description_en", DESCRIPTION_EN), ("version", version), ("author", author)):
        if scalar(wb_header, name) != expected:
            raise ValueError("WorkBuddy frontmatter check failed: " + name)
    prefix = NAME + "-v" + version
    outputs = {prefix + ".zip": archive_bytes(files), prefix + "-workbuddy.zip": archive_bytes(workbuddy)}
    verify_archive(outputs[prefix + ".zip"], files)
    verify_archive(outputs[prefix + "-workbuddy.zip"], workbuddy)
    hashes = "".join(hashlib.sha256(data).hexdigest() + "  " + name + "\n" for name, data in outputs.items())
    outputs[prefix + ".sha256"] = hashes.encode("ascii")
    out_dir.mkdir(parents=True, exist_ok=True)
    if any((out_dir / name).exists() or (out_dir / name).is_symlink() for name in outputs):
        raise FileExistsError("Release output already exists; choose a new version or --out-dir.")
    created = []
    try:
        for name, data in outputs.items():
            path = out_dir / name
            with path.open("xb") as handle:
                created.append(path)
                handle.write(data)
        for name, expected in outputs.items():
            if (out_dir / name).read_bytes() != expected:
                raise OSError("Release output verification failed: " + name)
    except OSError:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    print("Verified: universal 8 files; WorkBuddy 7 files; ZIP whitelist, byte parity, shared body and SHA-256.")
    for name in outputs:
        print(out_dir / name)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=ROOT / "dist", help="Output directory (default: repository dist).")
    args = parser.parse_args(argv)
    try:
        build(args.out_dir.resolve())
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print("Build failed: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
