"""Dashboard v0.17 features: actual cost, research map, compare, briefs, audio.

Kept out of server.py so the HTTP plumbing stays small. Everything here reads
the same SQLite history; Gemini is only called when the user asks for a
brief, a comparison summary, or audio.
"""

import io
import json
import math
import re
import sqlite3
import subprocess
import wave
from datetime import datetime
from pathlib import Path

# Gemini list prices, USD per 1M tokens (ai.google.dev/gemini-api/docs/pricing,
# checked 2026-09-26). The Deep Research agent bills model inference at the
# Gemini 3.1 Pro rate plus Google Search grounding per query.
PRO_INPUT_1M = 2.00
PRO_CACHED_1M = 0.20
PRO_OUTPUT_1M = 12.00
SEARCH_FREE_PER_MONTH = 5000
SEARCH_PER_1K = 14.00
# gemini-3.8-flash-tts launch rate through 2026-12-31: $0.50 text in, $9.00 audio
# out per 1M tokens; audio is 25 tokens per second.
TTS_MODEL = "gemini-3.8-flash-tts"
TTS_IN_1M = 0.50
TTS_OUT_1M = 9.00
TTS_TOKENS_PER_SEC = 25
# gemini-3.8-flash for summaries/briefs: $0.75 in, $3.75 out per 1M (2026 rate).
FLASH_MODEL = "gemini-3.8-flash"
FLASH_IN_1M = 0.75
FLASH_OUT_1M = 3.75

VOICES = ["Charon", "Kore", "Puck", "Aoede", "Fenrir", "Leda", "Orus", "Zephyr"]


def usage_cost(usage: dict | None) -> dict | None:
    """Turn an Interactions `usage` block into tokens and a dollar figure."""
    if not usage:
        return None
    total_in = int(usage.get("total_input_tokens") or 0)
    cached = int(usage.get("total_cached_tokens") or 0)
    out = int(usage.get("total_output_tokens") or 0)
    thought = int(usage.get("total_thought_tokens") or 0)
    tool = int(usage.get("total_tool_use_tokens") or 0)
    searches = sum(
        int(g.get("count") or 0)
        for g in usage.get("grounding_tool_count") or []
        if g.get("type") == "google_search"
    )
    fresh_in = max(total_in - cached, 0) + tool
    model_cost = (
        fresh_in / 1e6 * PRO_INPUT_1M
        + cached / 1e6 * PRO_CACHED_1M
        + (out + thought) / 1e6 * PRO_OUTPUT_1M
    )
    return {
        "started": usage.get("_created") or None,
        "finished": usage.get("_updated") or None,
        "input_tokens": total_in,
        "cached_tokens": cached,
        "output_tokens": out,
        "thought_tokens": thought,
        "tool_tokens": tool,
        "searches": searches,
        "model_usd": round(model_cost, 4),
        # Searches are free up to 5,000/month across all Gemini 3 use; shown
        # separately rather than guessed into the total.
        "search_usd_if_over_free": round(searches / 1000 * SEARCH_PER_1K, 4),
    }


class Features:
    def __init__(self, db_path: str, config_factory, audio_dir: Path):
        self.db_path = db_path
        self._config = config_factory
        self.audio_dir = audio_dir
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS session_usage (
                    session_id INTEGER PRIMARY KEY,
                    usage TEXT,
                    fetched_at TEXT,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS audio_exports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    ref_id INTEGER NOT NULL,
                    mode TEXT NOT NULL,
                    voice TEXT NOT NULL,
                    path TEXT NOT NULL,
                    seconds REAL,
                    cost_usd REAL,
                    script TEXT,
                    created_at TEXT
                );
                """
            )

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _client(self):
        # One long-lived client: a temporary one is garbage-collected (and its
        # HTTP session closed) before lazily-evaluated calls finish.
        if getattr(self, "_genai", None) is None:
            from google import genai

            self._genai = genai.Client(api_key=self._config().api_key)
        return self._genai

    # ---- actual cost --------------------------------------------------------

    def usage(self, sid: int, interaction_id: str, status: str, refresh=False):
        """Actual token usage for a session, cached once the run has finished."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT usage, error FROM session_usage WHERE session_id = ?", (sid,)
            ).fetchone()
        # Only a definitive answer is cached: real usage, or Google saying the
        # interaction has expired. Transient errors are retried next time.
        if (
            row
            and not refresh
            and status != "running"
            and (row["usage"] or (row["error"] or "").startswith("not available"))
        ):
            u = json.loads(row["usage"]) if row["usage"] else None
            return {"usage": usage_cost(u), "error": row["error"]}
        if not interaction_id or interaction_id.startswith("pending"):
            return {"usage": None, "error": "no interaction yet"}
        u, err = None, None
        try:
            it = self._client().interactions.get(interaction_id)
            d = it.model_dump(exclude_none=True) if hasattr(it, "model_dump") else {}
            u = d.get("usage")
            if u is not None:
                u = dict(
                    u,
                    _created=str(d.get("created") or ""),
                    _updated=str(d.get("updated") or ""),
                )
        except Exception as e:  # expired interactions return 404
            err = (
                "not available from Google (interaction expired)"
                if "404" in str(e)
                else str(e)[:200]
            )
        definitive = u is not None or (err or "").startswith("not available")
        if status != "running" and definitive:
            with self._conn() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO session_usage VALUES (?, ?, ?, ?)",
                    (
                        sid,
                        json.dumps(u) if u else None,
                        datetime.now().isoformat(timespec="seconds"),
                        err,
                    ),
                )
        return {"usage": usage_cost(u), "error": err}

    # ---- research map --------------------------------------------------------

    def research_map(self, limit_edges: int = 4, min_sim: float = 0.72) -> dict:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, prompt, created_at, LENGTH(result) AS n, embedding "
                "FROM sessions WHERE embedding IS NOT NULL AND parent_id IS NULL "
                "AND status = 'completed' ORDER BY id"
            ).fetchall()
        ids, vecs, nodes = [], [], []
        for r in rows:
            try:
                v = json.loads(r["embedding"])
            except (TypeError, ValueError):
                continue
            norm = math.sqrt(sum(x * x for x in v)) or 1.0
            ids.append(r["id"])
            vecs.append([x / norm for x in v])
            nodes.append(
                {
                    "id": r["id"],
                    "prompt": r["prompt"],
                    "created_at": r["created_at"],
                    "chars": r["n"],
                }
            )
        # 2-D layout: project onto the top two principal directions (power
        # iteration, no numpy), then let the client relax it.
        n = len(vecs)
        coords = [[0.0, 0.0] for _ in range(n)]
        if n >= 3:
            dim = len(vecs[0])
            mean = [sum(v[k] for v in vecs) / n for k in range(dim)]
            cen = [[v[k] - mean[k] for k in range(dim)] for v in vecs]
            comps: list[list[float]] = []
            for _ in range(2):
                w = [((i * 7919) % 97) / 97.0 - 0.5 for i in range(dim)]
                for _ in range(25):
                    proj = [sum(a * b for a, b in zip(row, w)) for row in cen]
                    w = [sum(proj[i] * cen[i][k] for i in range(n)) for k in range(dim)]
                    for c in comps:
                        dot = sum(a * b for a, b in zip(w, c))
                        w = [a - dot * b for a, b in zip(w, c)]
                    nw = math.sqrt(sum(x * x for x in w)) or 1.0
                    w = [x / nw for x in w]
                comps.append(w)
            for i, row in enumerate(cen):
                coords[i] = [sum(a * b for a, b in zip(row, c)) for c in comps]
        edges = []
        for i in range(n):
            sims = []
            for j in range(n):
                if i != j:
                    s = sum(a * b for a, b in zip(vecs[i], vecs[j]))
                    if s >= min_sim:
                        sims.append((s, j))
            sims.sort(reverse=True)
            for s, j in sims[:limit_edges]:
                a, b = sorted((ids[i], ids[j]))
                edges.append((a, b, round(s, 3)))
        uniq = {(a, b): s for a, b, s in edges}
        for i, node in enumerate(nodes):
            node["x"], node["y"] = coords[i]
        return {
            "nodes": nodes,
            "edges": [{"a": a, "b": b, "sim": s} for (a, b), s in uniq.items()],
            "indexed": n,
        }

    # ---- compare --------------------------------------------------------------

    @staticmethod
    def source_labels(md: str) -> set[str]:
        labels = set()
        for label, url in re.findall(r"\[([^\]]{1,200})\]\((https?://[^\s)]+)\)", md):
            if re.match(r"^\d+$", label.strip()):
                continue
            labels.add(label.strip().lower())
        return labels

    def compare(self, a: dict, b: dict, summarize: bool) -> dict:
        sa, sb = (
            self.source_labels(a.get("result") or ""),
            self.source_labels(b.get("result") or ""),
        )
        out = {
            "a": a["id"],
            "b": b["id"],
            "sources_only_a": sorted(sa - sb),
            "sources_only_b": sorted(sb - sa),
            "sources_shared": sorted(sa & sb),
            "summary": None,
        }
        if summarize:
            prompt = (
                "Compare two research reports on the same question. Report A is older, "
                "report B is newer. In Markdown, list: 1) New in B (findings not in A), "
                "2) No longer supported (claims in A that B contradicts or drops), "
                "3) Unchanged key conclusions. Be specific and short. Refer to reports "
                "as A and B.\n\n"
                f"--- REPORT A (#{a['id']}) ---\n{_strip_sources(a.get('result') or '')[:60000]}\n\n"
                f"--- REPORT B (#{b['id']}) ---\n{_strip_sources(b.get('result') or '')[:60000]}\n"
            )
            resp = self._client().models.generate_content(
                model=FLASH_MODEL, contents=prompt
            )
            out["summary"] = resp.text
        return out

    # ---- briefs ------------------------------------------------------------

    BRIEF_STYLES = {
        "brief": "a one-page executive brief: a two-sentence bottom line, then 4-6 key "
        "findings as bullets, then risks or open questions, then recommended next steps",
        "slides": "a slide outline: 6-10 slides, each with a title and 3-5 short bullets, "
        "plus a line of speaker notes",
        "email": "a short email to colleagues summarizing the findings, plain and direct, "
        "with a subject line",
    }

    def brief(self, title: str, content: str, style: str) -> dict:
        if style not in self.BRIEF_STYLES:
            raise ValueError(f"style must be one of {list(self.BRIEF_STYLES)}")
        prompt = (
            f"Turn these research notes into {self.BRIEF_STYLES[style]}. Output Markdown. "
            "Keep every citation marker exactly as written, e.g. '*Session #12*' or "
            "'[cite: 3]', attached to the claim it supports. Do not add facts that are "
            "not in the notes.\n\n"
            f"TITLE: {title}\n\nNOTES:\n{content[:120000]}"
        )
        resp = self._client().models.generate_content(
            model=FLASH_MODEL, contents=prompt
        )
        u = getattr(resp, "usage_metadata", None)
        cost = None
        if u:
            cost = round(
                (u.prompt_token_count or 0) / 1e6 * FLASH_IN_1M
                + (u.candidates_token_count or 0) / 1e6 * FLASH_OUT_1M,
                4,
            )
        return {"markdown": resp.text, "cost_usd": cost}

    # ---- audio ---------------------------------------------------------------

    @staticmethod
    def speakable(md: str) -> str:
        """Report Markdown -> plain text a voice can read word for word."""
        text = _strip_sources(md)
        text = re.sub(r"```.*?```", " ", text, flags=re.S)
        text = re.sub(r"\[cite:[^\]]*\]", "", text)
        text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
        text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
        text = re.sub(r"https?://\S+", "", text)
        text = re.sub(r"^\s*\|?[-:| ]{3,}\|?\s*$", "", text, flags=re.M)
        text = text.replace("|", ", ")
        text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)
        text = re.sub(r"[*_`>#]+", "", text)
        text = re.sub(r"^\s*[-+]\s+", "", text, flags=re.M)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @staticmethod
    def chunks(text: str, limit: int = 3500) -> list[str]:
        out, cur = [], ""
        for para in re.split(r"\n\s*\n", text):
            para = para.strip()
            if not para:
                continue
            while len(para) > limit:
                cut = para.rfind(". ", 0, limit)
                cut = cut + 1 if cut > limit // 2 else limit
                out.append(para[:cut].strip())
                para = para[cut:].strip()
            if cur and len(cur) + len(para) + 2 > limit:
                out.append(cur)
                cur = para
            else:
                cur = f"{cur}\n\n{para}" if cur else para
        if cur:
            out.append(cur)
        return out

    def estimate_audio(self, text: str, mode: str) -> dict:
        words = len(text.split())
        if mode == "summary":
            words = min(words, 450)
        seconds = words / 2.5  # ~150 words per minute
        cost = (
            len(text) / 4 / 1e6 * TTS_IN_1M
            + seconds * TTS_TOKENS_PER_SEC / 1e6 * TTS_OUT_1M
        )
        if mode == "summary":
            cost += len(text) / 4 / 1e6 * FLASH_IN_1M + 700 / 1e6 * FLASH_OUT_1M
        return {"words": words, "seconds": round(seconds), "cost_usd": round(cost, 3)}

    def summary_script(self, title: str, md: str) -> str:
        prompt = (
            "Write a spoken audio briefing of this research report for someone listening "
            "on the go. 2 to 3 minutes when read aloud (about 300-400 words). Warm, clear, "
            "plain spoken English: no Markdown, no bullet symbols, no URLs, no citation "
            "markers. Start with the question, then the main findings, then what it means. "
            "Spell out abbreviations the first time.\n\n"
            f"QUESTION: {title}\n\nREPORT:\n{_strip_sources(md)[:120000]}"
        )
        return (
            self._client()
            .models.generate_content(model=FLASH_MODEL, contents=prompt)
            .text.strip()
        )

    def synthesize(self, text: str, voice: str) -> bytes:
        """TTS each chunk with Gemini and join into one 24 kHz mono WAV."""
        from google.genai import types

        client = self._client()
        cfg = types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)
                )
            ),
        )
        pcm = bytearray()
        rate = 24000
        for part in self.chunks(text):
            last = None
            for _ in range(3):
                try:
                    resp = client.models.generate_content(
                        model=TTS_MODEL,
                        contents=f"Read this aloud:\n\n{part}",
                        config=cfg,
                    )
                    data = resp.candidates[0].content.parts[0].inline_data
                    raw = data.data
                    mime = data.mime_type or ""
                    if raw[:4] == b"RIFF":
                        with wave.open(io.BytesIO(raw)) as w:
                            rate = w.getframerate()
                            raw = w.readframes(w.getnframes())
                    else:
                        m = re.search(r"rate=(\d+)", mime)
                        rate = int(m.group(1)) if m else rate
                    pcm += raw + b"\x00\x00" * int(rate * 0.35)
                    last = None
                    break
                except Exception as e:  # transient 5xx/429
                    last = e
            if last:
                raise last
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(bytes(pcm))
        return buf.getvalue()

    def make_audio(
        self, kind: str, ref_id: int, title: str, md: str, mode: str, voice: str
    ) -> dict:
        if mode not in ("full", "summary"):
            raise ValueError("mode must be full or summary")
        if voice not in VOICES:
            raise ValueError(f"voice must be one of {VOICES}")
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM audio_exports WHERE kind=? AND ref_id=? AND mode=? "
                "AND voice=? ORDER BY id DESC LIMIT 1",
                (kind, ref_id, mode, voice),
            ).fetchone()
        if row and Path(row["path"]).exists():
            return dict(row) | {"cached": True}
        script = (
            self.summary_script(title, md) if mode == "summary" else self.speakable(md)
        )
        if not script:
            raise ValueError("Nothing to read")
        wav = self.synthesize(script, voice)
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        base = self.audio_dir / f"{kind}_{ref_id}_{mode}_{voice}"
        wav_path = base.with_suffix(".wav")
        wav_path.write_bytes(wav)
        path = wav_path
        try:  # mp3 is ~10x smaller; keep wav if ffmpeg is missing
            mp3 = base.with_suffix(".mp3")
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-y",
                    "-i",
                    str(wav_path),
                    "-codec:a",
                    "libmp3lame",
                    "-qscale:a",
                    "4",
                    str(mp3),
                ],
                check=True,
                timeout=600,
            )
            wav_path.unlink()
            path = mp3
        except Exception:
            pass
        with wave.open(io.BytesIO(wav)) as w:
            seconds = w.getnframes() / w.getframerate()
        cost = (
            len(script) / 4 / 1e6 * TTS_IN_1M
            + seconds * TTS_TOKENS_PER_SEC / 1e6 * TTS_OUT_1M
        )
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO audio_exports (kind, ref_id, mode, voice, path, seconds, "
                "cost_usd, script, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    kind,
                    ref_id,
                    mode,
                    voice,
                    str(path),
                    round(seconds, 1),
                    round(cost, 4),
                    script,
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            conn.commit()
            aid = cur.lastrowid
        return {
            "id": aid,
            "kind": kind,
            "ref_id": ref_id,
            "mode": mode,
            "voice": voice,
            "path": str(path),
            "seconds": round(seconds, 1),
            "cost_usd": round(cost, 4),
            "script": script,
            "cached": False,
        }

    def audio_file(self, aid: int) -> tuple[bytes, str, str]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM audio_exports WHERE id=?", (aid,)
            ).fetchone()
        if not row or not Path(row["path"]).exists():
            raise FileNotFoundError(aid)
        p = Path(row["path"])
        ctype = "audio/mpeg" if p.suffix == ".mp3" else "audio/wav"
        return p.read_bytes(), ctype, p.name

    def list_audio(self, kind: str, ref_id: int) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, mode, voice, seconds, cost_usd, created_at, path FROM "
                "audio_exports WHERE kind=? AND ref_id=? ORDER BY id DESC",
                (kind, ref_id),
            ).fetchall()
        return [dict(r) for r in rows if Path(r["path"]).exists()]


def _strip_sources(md: str) -> str:
    """Drop the trailing numbered source list (it is links, not prose)."""
    m = re.search(r"\n\*\*Sources:?\*\*\s*\n", md)
    if m:
        return md[: m.start()]
    m = re.search(r"\n#+\s*Sources\s*\n", md)
    return md[: m.start()] if m else md
