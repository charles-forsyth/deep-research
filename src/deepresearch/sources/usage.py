"""Data sources for research runs and follow-up questions (Ask).

Research: each chosen source is fetched on this machine into a folder and passed to
the run as an upload, so it lands in the run's temporary File Search Store (the same
path `--upload` uses; the store is deleted when the run ends). The run records which
sources and manifest hashes it used.

Follow-up / Ask: sources are small text context, so their text goes straight into the
prompt with a label, capped per source and in total. Anything larger belongs in a
research run or a Lab run.
"""

from __future__ import annotations

import atexit
import shutil
import tempfile
from pathlib import Path

from deepresearch.sources.adapters import SourceError, adapter_for
from deepresearch.sources.model import DataSource
from deepresearch.sources.registry import SourceRegistry

RESEARCH_MAX_FILES = 200  # File Search uploads per source
RESEARCH_MAX_BYTES = 200 * 1024**2
ASK_MAX_BYTES_PER_SOURCE = 60_000
ASK_MAX_BYTES_TOTAL = 200_000
TEXT_EXT = {
    "txt", "md", "csv", "tsv", "json", "jsonl", "yaml", "yml", "xml", "html", "htm",
    "py", "r", "sh", "sql", "log", "ini", "toml", "cfg", "tex", "rst", "ipynb",
}  # fmt: skip
UPLOAD_EXT = TEXT_EXT | {"pdf", "docx", "pptx", "xlsx"}


def resolve(registry: SourceRegistry, names: list[str]) -> list[DataSource]:
    out = []
    for n in names:
        s = registry.get(n)
        if s is None:
            raise SourceError(f"data source '{n}' does not exist")
        out.append(s)
    return out


def research_uploads(
    sources: list[DataSource], workdir: Path | None = None
) -> tuple[list[str], list[str]]:
    """Fetch sources into local folders for a research run.

    Returns (paths to upload, notes). Only file types File Search reads are kept; a
    source over the size or file cap is refused rather than silently cut.
    """
    if workdir is None:
        # Removed when the interpreter exits (the run needs the files until the
        # upload finishes, which is later than this function returns).
        tmp = tempfile.mkdtemp(prefix="dr-sources-")
        atexit.register(shutil.rmtree, tmp, True)
        workdir = Path(tmp)
    base = Path(workdir)
    paths: list[str] = []
    notes: list[str] = []
    for s in sources:
        m = s.manifest
        if m and m.total_bytes > RESEARCH_MAX_BYTES:
            raise SourceError(
                f"{s.name} is {m.total_bytes / 1024**2:.0f} MB; research runs take up to "
                f"{RESEARCH_MAX_BYTES // 1024**2} MB per source (use a Lab run for big data)"
            )
        dest = adapter_for(s).fetch(base / s.name)
        files = [
            p
            for p in sorted(dest.rglob("*"))
            if p.is_file() and p.suffix.lower().lstrip(".") in UPLOAD_EXT
        ]
        skipped = sum(1 for p in dest.rglob("*") if p.is_file()) - len(files)
        if len(files) > RESEARCH_MAX_FILES:
            raise SourceError(
                f"{s.name} has {len(files)} readable files; research runs take up to "
                f"{RESEARCH_MAX_FILES} per source (narrow it with --include)"
            )
        # the uploader reads one folder level, so flatten into one folder per source
        flat = base / f"{s.name}__flat"
        flat.mkdir(parents=True, exist_ok=True)
        for p in files:
            rel = str(p.relative_to(dest)).replace("/", "__")
            (flat / rel).write_bytes(p.read_bytes())
        if files:
            paths.append(str(flat))
        notes.append(
            f"{s.name}: {len(files)} file(s)"
            + (f", {skipped} skipped (not a readable type)" if skipped else "")
        )
    return paths, notes


def ask_context(sources: list[DataSource]) -> str:
    """Labelled text of each source for a follow-up prompt, within the caps."""
    parts: list[str] = []
    total = 0
    for s in sources:
        a = adapter_for(s)
        m = s.manifest or a.test()
        texts: list[str] = []
        used = 0
        for e in m.entries:
            if used >= ASK_MAX_BYTES_PER_SOURCE or total + used >= ASK_MAX_BYTES_TOTAL:
                texts.append("[... more files not included]")
                break
            if e.path.rsplit(".", 1)[-1].lower() not in TEXT_EXT and s.kind not in (
                "report",
                "notebook",
                "web",
            ):
                continue
            room = min(
                ASK_MAX_BYTES_PER_SOURCE - used, ASK_MAX_BYTES_TOTAL - total - used
            )
            try:
                raw = a.preview(
                    ""
                    if s.kind in ("web", "report", "notebook", "local_file")
                    else e.path,
                    room,
                )
            except SourceError as err:
                texts.append(f"[{e.path}: {err}]")
                continue
            body = raw.decode("utf-8", "replace")
            texts.append(f"--- {e.path} ---\n{body}")
            used += len(raw)
            if s.kind in ("web", "report", "notebook", "local_file"):
                break
        total += used
        parts.append(
            f'<data_source name="{s.name}" kind="{s.kind}" uri="{s.uri}">\n'
            + "\n".join(texts)
            + "\n</data_source>"
        )
    return "\n\n".join(parts)


def ask_prompt(question: str, sources: list[DataSource]) -> str:
    if not sources:
        return question
    return (
        "Use the data sources below together with the report when you answer. Cite a "
        "source by its name when you use it, and say plainly when they do not contain "
        "the answer.\n\n" + ask_context(sources) + f"\n\nQuestion: {question}"
    )
