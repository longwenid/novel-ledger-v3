"""On-demand historical recall by entity, event, topic and complete prose passages."""
from __future__ import annotations

import json
from typing import Any

from ..infra.util import LedgerError, ok


def recall(store, *, before_chapter: int, people: list[str] | None = None,
           topics: list[str] | None = None, assets: list[str] | None = None,
           event_ids: list[str] | None = None, query: str = "", limit: int = 12,
           max_chars: int | None = None) -> dict[str, Any]:
    store.load_config()
    before = int(before_chapter)
    if before < 1 or int(limit) < 1:
        raise LedgerError("invalid_recall_bounds", "before_chapter>=1 and a positive retrieval page size are required")
    names = list(dict.fromkeys(str(n).strip() for n in (people or []) if str(n).strip()))
    topics = list(dict.fromkeys(topics or []))
    assets = list(dict.fromkeys(assets or []))
    refs = list(dict.fromkeys(event_ids or []))
    page_size = max(int(limit), len(refs))
    # Even a caller asking about a future chapter may only see committed history.
    cutoff = min(before, int(store.read_head().get("last_committed_ch") or 0) + 1)
    with store.database.connection() as conn:
        matches, params = [], [cutoff]
        for values, expression in ((names, "ee.entity"), (topics, "m.topic"), (assets, "m.asset"), (refs, "m.event_id")):
            if values:
                placeholders = ",".join("?" for _ in values)
                if expression == "ee.entity":
                    matches.append(f"EXISTS(SELECT 1 FROM event_entities ee WHERE ee.event_id=m.event_id AND ee.entity IN ({placeholders}))")
                else:
                    matches.append(f"{expression} IN ({placeholders})")
                params.extend(values)
        if query.strip():
            matches.append("(instr(m.text,?)>0 OR instr(m.evidence,?)>0)")
            params.extend([query.strip()] * 2)
        clause = " AND (" + " OR ".join(matches) + ")" if matches else ""
        # Explicit refs are retrieved first; shared actors outrank single-person
        # records, then recent records. Fetch is capped before rendering evidence.
        score, order_params = "(0+0)", []
        if names:
            score = f"(SELECT count(*) FROM event_entities ee WHERE ee.event_id=m.event_id AND ee.entity IN ({','.join('?' for _ in names)}))"
            order_params.extend(names)
        ref_score = "(0+0)"
        if refs:
            ref_score = f"CASE WHEN m.event_id IN ({','.join('?' for _ in refs)}) THEN 1 ELSE 0 END"
            order_params = refs + order_params
        sql = f"SELECT m.* FROM memories m WHERE m.chapter<?{clause} ORDER BY {ref_score} DESC,{score} DESC,m.chapter DESC,m.id LIMIT ?"
        rows = list(conn.execute(sql, params + order_params + [page_size * 3]))
        missing = []
        for ref in refs:
            if not conn.execute("SELECT 1 FROM events WHERE id=? AND chapter<?", (ref, cutoff)).fetchone():
                missing.append(ref)
        if missing:
            raise LedgerError("missing_memory_ref", "explicit event references are missing or outside committed history", {"event_ids": missing})
        # Reserve one record per explicit event before filling remaining slots;
        # a verbose event cannot crowd a second reference out of the SQL window.
        reserved = []
        for ref in refs:
            row = conn.execute("SELECT * FROM memories WHERE event_id=? AND chapter<? ORDER BY (length(evidence)>0) DESC,(kind='event') ASC,id LIMIT 1", (ref, cutoff)).fetchone()
            if row is not None:
                reserved.append(row)
        reserved_ids = {row["id"] for row in reserved}
        rows = reserved + [row for row in rows if row["id"] not in reserved_ids]
        records = []
        for row in rows:
            chapter = int(row["chapter"])
            current = conn.execute("SELECT prose_hash FROM chapters WHERE chapter=?", (chapter,)).fetchone()
            evidence = row["evidence"]
            valid = bool(evidence and current and conn.execute("SELECT 1 FROM chapters c JOIN documents d ON d.path=c.path WHERE c.chapter=? AND instr(CAST(d.body AS TEXT),?)>0 LIMIT 1", (chapter, evidence)).fetchone())
            details = json.loads(row["payload"])
            latest = True
            if row["kind"] == "knowledge":
                latest = not conn.execute("SELECT 1 FROM memories m JOIN events e ON e.id=m.event_id JOIN events source ON source.id=? WHERE m.kind='knowledge' AND m.who=? AND m.topic=? AND m.chapter<? AND e.seq>source.seq LIMIT 1", (row["event_id"], row["who"], row["topic"], cutoff)).fetchone()
            records.append({"id": row["id"], "event_id": row["event_id"], "chapter": chapter,
                            "kind": row["kind"], "who": row["who"], "topic": row["topic"],
                            "text": row["text"], "quote": evidence if valid else "",
                            "prose_path": str(store.chapter_md_path(chapter)),
                            "event_source_path": str(store.events_path),
                            "evidence_status": "verified" if valid else "record_only",
                            "prose_hash": current[0] if current else None,
                            "stance": details.get("stance"), "source": details.get("source"),
                            "latest_knowledge": latest if row["kind"] == "knowledge" else None})
        # Query original passages as a fallback for details never entered into
        # the structured ledger. FTS is lexical; do not label it semantic recall.
        if query.strip():
            needle = query.strip()
            has_fts = conn.execute("SELECT 1 FROM sqlite_master WHERE name='passage_fts'").fetchone()
            if len(needle) >= 3 and has_fts:
                phrase = '"' + needle.replace('"', '""') + '"'
                passages = list(conn.execute("SELECT p.* FROM passage_fts f JOIN passages p ON p.id=f.rowid WHERE passage_fts MATCH ? AND p.chapter<? ORDER BY rank,p.chapter DESC LIMIT ?", (phrase, cutoff, int(limit))))
            else:
                passages = list(conn.execute("SELECT * FROM passages WHERE chapter<? AND instr(text,?)>0 ORDER BY chapter DESC LIMIT ?", (cutoff, needle, int(limit))))
            for p in passages:
                text = p["text"]
                records.append({"id": f"passage:{p['chapter']}:{p['ordinal']}", "chapter": p["chapter"],
                                "kind": "passage", "text": text, "quote": text,
                                "prose_path": str(store.chapter_md_path(int(p["chapter"]))),
                                "evidence_status": "verified", "prose_hash": p["prose_hash"]})
    selected, chars = [], 0
    for record in records:
        size = len(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
        if len(selected) >= page_size:
            continue
        selected.append(record)
        chars += size
    covered = {record.get("event_id") for record in selected}
    if set(refs) - covered:
        raise LedgerError("missing_memory_ref", "explicit event has no retrievable historical record", {"event_ids": sorted(set(refs) - covered)})
    return ok(action="memory_recall", before_chapter=cutoff, records=selected,
              omitted=max(0, len(records) - len(selected)), chars=chars,
              max_chars=None, legacy_max_chars_ignored=max_chars,
              sources={"events": str(store.events_path), "database": str(store.database.path)},
              retrieval="relational_and_lexical")


def memory_for_chapter(store, chapter: int, chapter_plan: dict, present: list[str]) -> dict:
    caps = store.load_config().get("pack_caps") or {}
    count = int(caps.get("history_records", 8))
    refs = chapter_plan.get("memory_refs") or []
    if not isinstance(refs, list) or any(not isinstance(ref, str) or not ref for ref in refs):
        raise LedgerError("invalid_memory_refs", "memory_refs must be a list of event ids")
    if len(refs) != len(set(refs)):
        raise LedgerError("invalid_memory_refs", "memory_refs must be unique")
    if count <= 0 and not refs:
        return {}
    result = recall(store, before_chapter=chapter, people=present,
                    topics=chapter_plan.get("knowledge_refs") or [], event_ids=refs,
                    query=str(chapter_plan.get("memory_query") or ""),
                    limit=max(count, len(refs), 1))
    return {key: result[key] for key in ("records", "omitted", "chars", "before_chapter", "sources")}
