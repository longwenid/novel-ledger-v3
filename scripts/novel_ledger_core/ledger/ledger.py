from __future__ import annotations

import copy
import re
from typing import Any

from ..infra.store import BookStore
from ..infra.util import (
    LedgerError,
    atomic_json,
    atomic_text,
    canonical_json,
    coerce_due,
    now_ts,
    read_json,
    sha256_text,
)

EMPTY_SNAPSHOT: dict[str, Any] = {
    "chapter": 0,
    "entities": {},
    "debts": [],
    "hooks": [],
    "relations": [],
    "occupancy": {},
    "items": [],
    "conditions": [],
    "knowledge": [],
    "locations": [],
}

FACTS_PER_ENTITY = 12

# 关系条目的结束词汇。契约口径是 `open` / `closed`，但现场写法五花八门
# （达成 / 试探中 / 建立 / 已结束…），只认字面 "closed" 会让"已经结束的关系"继续算 open，
# 于是同一对角色越攒越多、永远收敛不了。这里给一个明确的同义词集合，两侧（冲突检测与关系切片）共用。
RELATION_CLOSED_TOKENS = frozenset({"closed", "close", "已关闭", "已结束", "结束", "关闭", "终止"})


def relation_is_closed(rel: Any) -> bool:
    """关系条目是否已结束（缺省 open）。"""
    if not isinstance(rel, dict):
        return False
    return str(rel.get("status") or "open").strip().lower() in {
        token.lower() for token in RELATION_CLOSED_TOKENS
    }

# 角色状态（伤势 / 体力 / 欠债 / 职务…）上限。**不可逆代价不受此限**——
# 断肢、毁容、资格吊销、破产清零这类东西的定义就是"它不会自己好"，
# 一旦因为条数上限被挤出 pack，写者下一章就会让断了的手重新握剑。
CONDITIONS_PER_ENTITY = 16
# 单章工作包最多承载的活跃不可逆状态。快照始终全量保留；超过此数时
# 停止装配并要求显式治理，避免隐去永久代价或让上下文随章数无界增长。
IRREVERSIBLE_CONDITIONS_PACK_MAX = 32

# 道具状态词表（见 write-output「道具账本」）：转交给新持有人时快照归一为 held。
# used 已消耗、未指明新持有人的 transferred、lost/destroyed 已失去，不作为在持资产。
_ITEM_NOT_HELD_STATUSES = frozenset({"used", "transferred", "lost", "destroyed", "exhausted"})


def _norm_fact(fact: Any) -> dict[str, Any] | None:
    """事实统一形状 {text, pin}。非对象或没有 text 的条目丢弃。"""
    if not isinstance(fact, dict):
        return None
    text = str(fact.get("text") or "").strip()
    if not text:
        return None
    return {"text": text, "pin": bool(fact.get("pin") or False)}


def _normalize_due(entity: dict[str, Any], kind: str) -> dict[str, Any]:
    """debts/hooks delta 条目的 due 归一：可判读 → int；不可判读 → 移除该键。

    读侧（到期检查、select_hooks）一律走 coerce_due 宽容归一，不再裸 int()；
    归一只救可判读的值，不掩盖语义不明——解析失败的 due 视作「未声明」直接剥掉，
    避免「第 N 章」等旧契约文本毒值入快照。
    """
    item = dict(entity)
    if "due" in item:
        coerced = coerce_due(item.get("due"))
        if coerced is None:
            item.pop("due", None)
        else:
            item["due"] = coerced
    return item


def _evict_facts(facts: list[dict[str, Any]], cap: int = FACTS_PER_ENTITY) -> list[dict[str, Any]]:
    """pinned 事实永不驱逐；非 pinned 保留最近若干条，总量不超过 cap。"""
    # 相同事实反复被申报时只留一条，pin 只能升级不能被后续普通申报降级。
    # 最近一次申报决定排序；否则千章重提同一铁律会占满 pack 的三个展示槽。
    unique: dict[str, dict[str, Any]] = {}
    for fact in facts:
        key = fact["text"]
        previous = unique.pop(key, None)
        unique[key] = {"text": key, "pin": bool(fact.get("pin") or (previous or {}).get("pin"))}
    facts = list(unique.values())
    pinned = [f for f in facts if f.get("pin")]
    unpinned = [f for f in facts if not f.get("pin")]
    keep_unpinned = max(0, cap - len(pinned))
    return pinned + (unpinned[-keep_unpinned:] if keep_unpinned else [])


def _fact_terms(text: str, who: str = "") -> set[str]:
    """提取短词重叠；去掉人物本名，避免所有事实只因人名命中章拍。"""
    normalized = text.lower().replace(who.lower(), "") if who else text.lower()
    terms: set[str] = set()
    for run in re.findall(r"[\u3400-\u9fff]+|[a-z0-9_]+", normalized):
        if not re.match(r"[\u3400-\u9fff]", run):
            if len(run) >= 3:
                terms.add(run)
            continue
        for size in range(2, min(4, len(run)) + 1):
            terms.update(run[i : i + size] for i in range(len(run) - size + 1))
    return terms


def facts_for_pack(ent_facts: Any, cap: int = 3, *, focus: str = "", who: str = "") -> list[str]:
    """每人最多 cap 条；章拍词面相关者优先，其余沿用最近 pinned 优先。"""
    if cap <= 0:
        return []
    facts = [f for f in (_norm_fact(x) for x in (ent_facts or [])) if f]
    pinned = [f for f in facts if f["pin"]]
    unpinned = [f for f in facts if not f["pin"]]
    focus_terms = _fact_terms(focus, who) if focus else set()
    if focus_terms:
        scored: list[tuple[bool, int, int, int]] = []
        for index, fact in enumerate(facts):
            hits = _fact_terms(fact["text"], who) & focus_terms
            if hits:
                scored.append((fact["pin"], max(len(hit) for hit in hits), len(hits), index))
        if scored:
            scored.sort(reverse=True)
            selected: list[int] = [index for _, _, _, index in scored[:cap]]
            seen = set(selected)
            for index in reversed(range(len(facts))):
                if len(selected) >= cap:
                    break
                if facts[index]["pin"] and index not in seen:
                    selected.append(index)
                    seen.add(index)
            for index in reversed(range(len(facts))):
                if len(selected) >= cap:
                    break
                if index not in seen:
                    selected.append(index)
                    seen.add(index)
            return [facts[index]["text"] for index in selected]
    shown = pinned[-cap:]
    remaining = cap - len(shown)
    if remaining:
        shown += unpinned[-remaining:]
    return [f["text"] for f in shown]


_CONDITION_STATUS_DEFAULT = "active"


def _norm_condition(cond: Any) -> dict[str, Any] | None:
    """角色状态统一形状：{who, kind, text, value?, unit?, irreversible?, status, quote?}。

    `kind` 是**项目自定**的自由标签（伤势 / 体力 / 欠债 / 职务…）：制度层不携带任何一本书的
    世界观，所以这里只提供容器与纪律，词汇由项目正典注入。`irreversible` 是唯一的硬语义。
    """
    if not isinstance(cond, dict):
        return None
    text = str(cond.get("text") or "").strip()
    who = str(cond.get("who") or "").strip()
    if not text or not who:
        return None
    out: dict[str, Any] = {
        "who": who,
        "kind": str(cond.get("kind") or "other").strip() or "other",
        "text": text,
        "status": str(cond.get("status") or _CONDITION_STATUS_DEFAULT).strip().lower()
        or _CONDITION_STATUS_DEFAULT,
    }
    if cond.get("value") is not None:
        out["value"] = cond["value"]
    if str(cond.get("unit") or "").strip():
        out["unit"] = str(cond["unit"]).strip()
    if bool(cond.get("irreversible")):
        out["irreversible"] = True
    if str(cond.get("quote") or "").strip():
        out["quote"] = str(cond["quote"]).strip()
    return out


def _condition_key(cond: dict[str, Any]) -> tuple[str, str, str]:
    return (cond["who"], cond["kind"], cond["text"])


def _evict_conditions(
    conditions: list[dict[str, Any]], cap: int = CONDITIONS_PER_ENTITY
) -> list[dict[str, Any]]:
    """不可逆代价永不驱逐；每人优先保留有效可逆状态。

    可逆状态每人最多 cap 条：先留最近的有效状态，剩余额度再留最近的
    resolved/healed/closed 记录。不可逆状态不占额度，角色之间也不共享额度。
    """
    protected = [c for c in conditions if c.get("irreversible")]
    remaining: dict[str, int] = {}
    chosen: set[int] = set()
    # 两次倒序扫描均为 O(n)：有效状态先占额度，结案记录只用剩余空间。
    for terminal in (False, True):
        for index in range(len(conditions) - 1, -1, -1):
            cond = conditions[index]
            if cond.get("irreversible"):
                continue
            is_terminal = str(cond.get("status") or "active").strip().lower() in (
                "resolved", "healed", "closed"
            )
            if is_terminal != terminal:
                continue
            who = str(cond.get("who") or "")
            quota = remaining.get(who, cap)
            if quota > 0:
                chosen.add(index)
                remaining[who] = quota - 1
    return protected + [cond for index, cond in enumerate(conditions) if index in chosen]


def select_conditions(
    store: BookStore,
    *,
    names: list[str],
    snapshot: dict[str, Any] | None = None,
    cap: int = CONDITIONS_PER_ENTITY,
    irreversible_cap: int = IRREVERSIBLE_CONDITIONS_PACK_MAX,
) -> tuple[list[dict[str, Any]], int]:
    """在场角色的当前状态切片，返回 `(条目, 被省略条数)`。

    在配置的不可逆状态上限内一律全量返回（不参与可逆状态 cap）；超限即停线，
    不静默隐藏永久代价。可逆状态在全局 cap 内按
    在场角色顺序轮流取各人的最近一条，避免单人独占。cap 小于有状态角色数时，
    先列出的角色优先。截断条数必须回传，让写者知道视野边界。
    """
    snap = snapshot if snapshot is not None else load_snapshot(store)
    ordered_names = list(dict.fromkeys(str(n).strip() for n in names if str(n).strip()))
    name_set = set(ordered_names)
    if not name_set:
        return [], 0
    alive: list[dict[str, Any]] = []
    for raw in snap.get("conditions") or []:
        cond = _norm_condition(raw)
        if not cond or cond["who"] not in name_set:
            continue
        if cond["status"] in ("resolved", "healed", "closed"):
            continue
        alive.append(cond)
    protected = [c for c in alive if c.get("irreversible")]
    if len(protected) > irreversible_cap:
        raise LedgerError(
            "irreversible_conditions_overflow",
            f"{len(protected)} active irreversible conditions for present characters exceed "
            f"pack_caps.irreversible_conditions={irreversible_cap}; run book audit to inspect "
            "them, temporarily raise this cap for a reconciliation chapter, explicitly "
            "close/resolved no-longer-active conditions in that chapter's state_delta, "
            "then restore the cap",
            {"count": len(protected), "cap": irreversible_cap},
        )
    rest = [c for c in alive if not c.get("irreversible")]
    by_who: dict[str, list[int]] = {}
    for index, cond in enumerate(rest):
        by_who.setdefault(cond["who"], []).append(index)
    chosen: set[int] = set()
    while len(chosen) < cap:
        progressed = False
        for who in ordered_names:
            indices = by_who.get(who)
            if not indices:
                continue
            chosen.add(indices.pop())
            progressed = True
            if len(chosen) >= cap:
                break
        if not progressed:
            break
    omitted = len(rest) - len(chosen)
    out = protected + [cond for index, cond in enumerate(rest) if index in chosen]
    return out, omitted


def _coerce_snapshot(data: dict[str, Any]) -> dict[str, Any]:
    """Validate the current snapshot schema against `EMPTY_SNAPSHOT`."""
    out: dict[str, Any] = {}
    for key, template in EMPTY_SNAPSHOT.items():
        # 旧项目的快照没有个人认知账；空列表表示尚未追踪，不能推断角色不知情。
        if key == "knowledge" and key not in data:
            out[key] = []
            continue
        # 地点簿（场景地图）同理：老书快照没有该键，回填空注册表——
        # 首次有人申报空间属性时自然建立，不需要迁移步骤。
        if key == "locations" and key not in data:
            out[key] = []
            continue
        if key not in data or data.get(key) is None:
            raise LedgerError("invalid_ledger", f"snapshot.{key} is required")
        value = data[key]
        if isinstance(template, dict) and not isinstance(value, dict):
            raise LedgerError("invalid_ledger", f"snapshot.{key} must be an object")
        if isinstance(template, list) and not isinstance(value, list):
            raise LedgerError("invalid_ledger", f"snapshot.{key} must be a list")
        out[key] = value
    if not isinstance(out.get("entities"), dict):
        raise LedgerError("invalid_ledger", "snapshot.entities must be an object")
    return out


def load_snapshot(store: BookStore) -> dict[str, Any]:
    if not store.snapshot_path.exists():
        return copy.deepcopy(EMPTY_SNAPSHOT)
    data = read_json(store.snapshot_path)
    if not isinstance(data, dict):
        raise LedgerError("invalid_ledger", "snapshot must be an object")
    out = _coerce_snapshot(data)
    out["chapter"] = int(data.get("chapter") or 0)
    return out


def replay_events(store: BookStore) -> dict[str, Any]:
    snap: dict[str, Any] = copy.deepcopy(EMPTY_SNAPSHOT)
    if not store.events_path.exists():
        return snap
    text = store.events_path.read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        event = read_json_line(line)
        snap = apply_event(snap, event)
    return snap


def read_json_line(line: str) -> dict[str, Any]:
    import json

    try:
        data = json.loads(line)
    except json.JSONDecodeError as exc:
        raise LedgerError("invalid_ledger", "events.jsonl has a broken line", str(exc)) from exc
    if not isinstance(data, dict):
        raise LedgerError("invalid_ledger", "ledger event must be an object")
    return data


def read_events(store: BookStore) -> list[dict[str, Any]]:
    """按写入顺序读出全部账本事件。"""
    if not store.events_path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in store.events_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            events.append(read_json_line(line))
    return events


def ledger_history_fingerprint(store: BookStore, before_chapter: int) -> str:
    """Seal ledger inputs preceding a chapter, including editorial rulings.

    The target chapter's own commit is an output, so it must not invalidate its
    pack while commit is resumed after a crash. Governance uses its effective
    chapter and therefore changes the next chapter's inputs immediately.
    """
    history = [
        event for event in read_events(store)
        if int(event.get("chapter") or event.get("effective_chapter") or 0) < int(before_chapter)
    ]
    return "sha256:" + sha256_text(canonical_json(history).decode("utf-8"))


def lifecycle_names(raw: Any, *, field: str) -> list[str]:
    """Validate the string/{who} declarations shared by death lifecycle fields."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise LedgerError("lifecycle_name_invalid", f"{field} must be a list of character names")
    names: list[str] = []
    for index, item in enumerate(raw):
        name = item.get("who") if isinstance(item, dict) else item
        if not isinstance(name, str) or not name.strip():
            raise LedgerError(
                "lifecycle_name_invalid", f"{field}[{index}] requires a non-empty character name",
                {"field": field, "index": index},
            )
        name = name.strip()
        if name in names:
            raise LedgerError(
                "lifecycle_duplicate_name", f"{field} repeats character {name!r}",
                {"field": field, "index": index, "who": name},
            )
        names.append(name)
    return names


def event_chapters(store: BookStore) -> list[int]:
    """Only chapter commits consume chapter numbers; governance events do not."""
    return [int(e.get("chapter") or 0) for e in read_events(store) if e.get("type") != "governance"]


# —— 事件哈希链 ————
# events.jsonl 是唯一真源，但裸 JSONL 被篡改后重放依然自洽，`ledger repair --from-events`
# 会把篡改内容"洗"进 snapshot 且无从察觉。事件写入时携带 prev_hash + hash 成链，verify
# 逐条重算。注意：这防的是"改中间"，不防"协同重写全部事件+快照"——那需要外部锚点。


def _event_digest(event: dict[str, Any]) -> str:
    """sha256(prev_hash + canonical(事件体))。事件体不含 hash 自身。

    显式排除 hash 键：对已封印的事件重算（如 restamp 后重封）得到与首次封印
    相同的摘要——否则旧 hash 会被卷进新 hash，verify 必然 mismatch。
    """
    body = {key: value for key, value in event.items() if key != "hash"}
    prefix = str(body.get("prev_hash") or "")
    return "sha256:" + sha256_text(prefix + canonical_json(body).decode("utf-8"))


def event_chain_issues(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """逐条重算哈希与 prev_hash 链接。空列表 = 链完整；无哈希事件即断链。"""
    issues: list[dict[str, Any]] = []
    prev = ""
    for index, event in enumerate(events):
        chapter = int(event.get("chapter") or event.get("effective_chapter") or 0)
        stored = event.get("hash")
        if not stored:
            issues.append({"code": "event_hash_missing", "index": index, "chapter": chapter})
            prev = ""
            continue
        recomputed = _event_digest(event)
        if recomputed != str(stored):
            issues.append({"code": "event_hash_mismatch", "index": index, "chapter": chapter})
        if str(event.get("prev_hash") or "") != prev:
            issues.append({"code": "event_chain_broken", "index": index, "chapter": chapter})
        prev = str(stored)
    return issues


def max_event_chapter(store: BookStore) -> int:
    return max(event_chapters(store), default=0)


def rollback_ledger_to(store: BookStore, chapter: int) -> dict[str, Any]:
    """撤销第 chapter 章及其后的全部账本事件，并按剩余事件重建快照。

    同章重写（ack p0 / retry-authorize）必须先调用它：否则废稿申报的
    facts/debts/hooks/relations 永久留在账本里，重写稿再 append 一条同章事件，
    同一实体下会出现"两稿并存"的事实，而废稿正文早已删除、无从排查。
    deaths 尤其不可逆（apply_event 只设 dead=True，无处清除），废稿里误杀的角色
    会永久死亡，后续任何涉及他的 delta 都会以 dead_speaking 停线。
    """
    chapter = int(chapter)
    events = read_events(store)
    # 治理事件不占章号，却依附于执行时的已提交章节；回滚该章也要撤销
    # 它之后的治理，否则撤回正文后会保留针对废稿资产的裁决。
    kept = [
        e for e in events
        if int(e.get("effective_chapter") if e.get("type") == "governance" else e.get("chapter") or 0)
        < chapter
    ]
    removed = len(events) - len(kept)
    body = "".join(canonical_json(e).decode("utf-8").rstrip("\n") + "\n" for e in kept)
    atomic_text(store.events_path, body)
    snap = replay_events(store)
    atomic_json(store.snapshot_path, snap)
    return {
        "rolled_back_to": chapter,
        "removed_events": removed,
        "kept_events": len(kept),
        "snapshot_chapter": int(snap.get("chapter") or 0),
    }


def repair_ledger(store: BookStore) -> dict[str, Any]:
    """用 events.jsonl 重放结果覆盖 snapshot.json（真源 → 派生视图）。

    verify_ledger 只读不修；这是它缺失的另一半：events.jsonl 是真源，
    snapshot 只是派生视图，被篡改/半写坏时按真源重建即可。
    """
    # 与 verify 同理：未初始化项目不能以"repaired: false"报成功，
    # 否则一次路径写错就会被读成"账本没问题"。
    store.read_head()
    # 链先验：真源哈希链断裂时拒绝修复。repair 的语义是"按真源重建快照"，
    # 链断说明真源本身可疑，此时重建等于把篡改内容洗白进 snapshot。
    chain_issues = event_chain_issues(read_events(store))
    if chain_issues:
        raise LedgerError(
            "event_chain_broken",
            "events.jsonl hash chain is broken; refusing to repair from a tampered truth source",
            chain_issues,
        )
    before = load_snapshot(store)
    replayed = replay_events(store)
    changed = [
        field
        for field in EMPTY_SNAPSHOT
        if before.get(field) != replayed.get(field)
    ]
    atomic_json(store.snapshot_path, replayed)
    return {
        "repaired": bool(changed),
        "changed_fields": changed,
        "events": len(read_events(store)),
        "snapshot_chapter": int(replayed.get("chapter") or 0),
        "snapshot_entities": len(replayed.get("entities") or {}),
    }


# 快照里以顶层列表存在的引文种类（引用随条目合并保留）。
_LEDGER_QUOTE_KINDS = ("debts", "hooks", "relations", "items", "conditions")
# 只在事件真源里留痕的种类：facts 经 _norm_fact 只存 {text,pin}，deaths 只存 {who,dead}，
# 快照会丢掉它们的 quote，所以必须回 events.jsonl 取锚，否则任何整章重写都能把它们悄悄改断。
_LEDGER_EVENT_QUOTE_KINDS = ("facts", "deaths", "knowledge")
_TERMINAL_STATUSES = {"paid", "cancelled", "closed", "resolved", "done", "settled"}


def _debt_active(debt: dict[str, Any]) -> bool:
    return str(debt.get("status") or "open").strip().lower() not in _TERMINAL_STATUSES


def _updated_chapter(entry: dict[str, Any]) -> int:
    try:
        return int(entry.get("updated_chapter") or 0)
    except (TypeError, ValueError):
        return 0


def ledger_quotes(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """取出账本里带正文引文（quote）的全部条目，供逐字校验。"""
    out: list[dict[str, Any]] = []
    for kind in _LEDGER_QUOTE_KINDS:
        for item in snapshot.get(kind) or []:
            if not isinstance(item, dict):
                continue
            quote = str(item.get("quote") or "").strip()
            if quote:
                out.append({"kind": kind, "id": str(item.get("id") or ""), "quote": quote})
    # 地点簿的引文锚在 attributes 里（每属性一份声明锚），按属性展开。
    for loc in snapshot.get("locations") or []:
        if not isinstance(loc, dict):
            continue
        for key, attr in (loc.get("attributes") or {}).items():
            quote = str((attr or {}).get("quote") or "").strip() if isinstance(attr, dict) else ""
            if quote:
                out.append({"kind": "locations", "id": f"{loc.get('id')}:{key}", "quote": quote})
    return out


def ledger_event_quotes(store: BookStore) -> list[dict[str, Any]]:
    """从事件真源取 facts/deaths 的引文锚（快照丢这两类的 quote）。

    同一引文在多次事件里重复出现时只报一次（按 kind+quote 去重，保留最早章号）。
    """
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for event in read_events(store):
        chapter = int(event.get("chapter") or 0)
        delta = event.get("state_delta") or {}
        for kind in _LEDGER_EVENT_QUOTE_KINDS:
            for item in delta.get(kind) or []:
                if not isinstance(item, dict):
                    continue
                quote = str(item.get("quote") or "").strip()
                if not quote:
                    continue
                key = (kind, quote)
                if key in seen:
                    continue
                seen.add(key)
                out.append(
                    {
                        "kind": kind,
                        "id": str(item.get("id") or item.get("topic_id") or item.get("who") or ""),
                        "chapter": chapter,
                        "quote": quote,
                    }
                )
    return out


def _all_ledger_quotes(store: BookStore, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """全套引文锚：快照四类（debts/hooks/relations/items/conditions）+ 事件真源两类（facts/deaths）。"""
    return ledger_quotes(snapshot) + ledger_event_quotes(store)


def _chapter_prose_map(store: BookStore) -> dict[int, str]:
    out: dict[int, str] = {}
    for cf in sorted(store.chapters_dir.glob("*/ch-*.md")):
        try:
            num = int(cf.stem.split("-")[1])
        except (IndexError, ValueError):
            continue
        out[num] = cf.read_text(encoding="utf-8")
    return out


def ledger_quote_issues(store: BookStore, snapshot: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """账本引文完整性：每条 debts/hooks/relations/items/conditions/facts/deaths 的 quote 必须逐字出现在某章已提交正文里。

    账本语义（金额、状态）由组装阶段的 LLM 从正文抽取，quote 是它的正文锚。正文一旦被
    整体改尺（如正典改价后回改）而账本没同步，锚就断了，错值会顺着 NOW 卡灌进后面每一章。
    facts/deaths 的 quote 不在快照里（只存 text / who），须回事件源取，否则整章重写会
    静默改断它们。这里只做检测（不改数据）：断锚意味着账本值与正文已对不上，须人工核账。
    """
    snap = snapshot if snapshot is not None else load_snapshot(store)
    prose_all = "\n".join(_chapter_prose_map(store).values())
    issues: list[dict[str, Any]] = []
    for entry in _all_ledger_quotes(store, snap):
        if entry["quote"] in prose_all:
            continue
        issues.append(
            {
                **entry,
                "hint": "账本 quote 不在任何已提交正文中：正文改后未同步账本，或组装时引文是幻觉。"
                "该条账本值不可信，须核对正文后修正账本。",
            }
        )
    return issues


def _id_stem(ident: str) -> str:
    """把 id 归一成主体词：去分隔符、大小写，剥掉 debt/hook 前缀与度量数字后缀。"""
    raw = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", str(ident or "")).lower()
    for prefix in ("debt", "hook", "d", "h"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break
    return re.sub(r"\d+", "", raw)


def ledger_hygiene_issues(store: BookStore, snapshot: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """账本身份卫生（advisory）：同一主体被不同 id 重复登记、同一 id 被多次置终态。

    同一件"销中人钱"若先记为 d_zhongren、后记为 debt_zhongren_300，就会在快照里并存两条，
    正文把同一笔账反复"划掉"也无人拦。这里按 id 主体词与终态迁移次数把这类重复挑出来。
    """
    snap = snapshot if snapshot is not None else load_snapshot(store)
    issues: list[dict[str, Any]] = []
    for kind in ("debts", "hooks"):
        groups: dict[str, set[str]] = {}
        for item in snap.get(kind) or []:
            if not isinstance(item, dict):
                continue
            ident = str(item.get("id") or "").strip()
            if ident:
                groups.setdefault(_id_stem(ident), set()).add(ident)
        for stem, ids in groups.items():
            if len(ids) > 1:
                issues.append(
                    {
                        "code": "duplicate_id_stem",
                        "kind": kind,
                        "stem": stem,
                        "ids": sorted(ids),
                        "hint": "同一主体疑似被不同 id 重复登记：确认是否同一笔账/同一伏笔，"
                        "合并为单一 id，避免正文反复叙事同一件事。",
                    }
                )

    terminal_events: dict[str, list[dict[str, Any]]] = {}
    for event in read_events(store):
        chapter = int(event.get("chapter") or 0)
        delta = event.get("state_delta") or {}
        # 只看债务：账被反复"销/划"才是重叙事信号；伏笔在后续章被顺带重列（状态不变）
        # 属正常写法，不该报。
        for item in delta.get("debts") or []:
            if not isinstance(item, dict):
                continue
            ident = str(item.get("id") or "").strip()
            status = str(item.get("status") or "").strip().lower()
            if ident and status in _TERMINAL_STATUSES:
                terminal_events.setdefault(ident, []).append({"chapter": chapter, "status": status})
    for ident, hits in terminal_events.items():
        distinct = sorted({(h["chapter"], h["status"]) for h in hits})
        if len(distinct) > 1:
            issues.append(
                {
                    "code": "terminal_state_repeated",
                    "id": ident,
                    "events": [{"chapter": c, "status": s} for c, s in distinct],
                    "hint": "同一账/伏笔被多次销案/结案：同一件事被反复当成新进展叙事，须合并或改写后续章。",
                }
            )

    # 同一对角色并存多条 open 关系：可能是合法多重身份（师徒＋姻亲），
    # 也可能是「盟友→仇敌」这类演变后旧条目忘了关。只报不改，交总编辑裁决。
    pairs: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for rel in snap.get("relations") or []:
        if not isinstance(rel, dict):
            continue
        if relation_is_closed(rel):
            continue
        who = str(rel.get("who") or "").strip()
        target = str(rel.get("target") or "").strip()
        kind = str(rel.get("kind") or "").strip()
        if not who or not target or not kind:
            continue
        pairs.setdefault(tuple(sorted((who, target))), []).append(
            {
                "who": who,
                "target": target,
                "kind": kind,
                "updated_chapter": int(rel.get("updated_chapter") or 0),
            }
        )
    for pair, entries in pairs.items():
        if len({e["kind"] for e in entries}) <= 1:
            continue
        issues.append(
            {
                "code": "relation_pair_conflict",
                "pair": list(pair),
                "relations": entries,
                "hint": "同一对角色同时挂着多条 open 关系。若其中一条已被后续剧情取代"
                "（盟友→仇敌、师徒→反目），在新的那条 state_delta.relations 上加 "
                '"supersedes": true（可再用 "supersedes_kind" 只退掉指定的一条），'
                "账本会把该对角色其它 open 条目置为 closed；"
                "若两种关系确实同时成立（如师徒＋姻亲），不要加 supersedes，忽略本条。",
            }
        )
    issues.extend(_relation_name_variant_issues(snap))
    return issues


def _relation_name_variant_issues(snap: dict[str, Any]) -> list[dict[str, Any]]:
    """关系表里同一人物的两种叫法（一个名字包含另一个）。

    现场曾出现：同一人先以带前缀的全名登记、后又以短名出现在关系里，于是关系表多出
    一条平行条目，`allowed_delta_names` 里也同时
    存在两个词——事实编辑每次都得在两个名字之间猜。只查关系表两端（范围最窄、证据最硬），
    命中交总编辑裁决：统一规范名，或确认是两个不同的人。
    """
    names: set[str] = set()
    for rel in snap.get("relations") or []:
        if not isinstance(rel, dict):
            continue
        for key in ("who", "target"):
            value = str(rel.get(key) or "").strip()
            if value:
                names.add(value)
    ordered = sorted(names)
    issues: list[dict[str, Any]] = []
    for index, shorter in enumerate(ordered):
        if len(shorter) < 2:
            continue
        for longer in ordered[index + 1 :]:
            if shorter in longer:
                issues.append(
                    {
                        "code": "relation_name_variant_suspected",
                        "shorter": shorter,
                        "longer": longer,
                        "hint": "关系表里出现互相包含的两个名字，疑似同一人物的两种叫法："
                        "它会各自累积一条关系，也会让 allowed_delta_names 同时出现两个词。"
                        "请总编辑裁决：同一人则 `relations rename --from <变体> --to <规范名>`"
                        "（追加裁决事件，保留原章记录），或确认是两个不同的人。",
                    }
                )
    return issues


def ledger_quote_orphans(
    store: BookStore,
    before_text: str,
    after_text: str,
    *,
    chapter: int | None = None,
) -> list[dict[str, Any]]:
    """找出"这次改写把原本还锚得住的账本引文改断了、且别处也补不回来"的条目。

    供 `chapter patch` 就地修润时 fail-closed：改数字/改措辞若让账本引文失去正文支撑，
    就不能静默通过——否则账本会长期停在旧值而无人察觉。
    """
    snapshot = load_snapshot(store)
    others = "\n".join(p for n, p in _chapter_prose_map(store).items() if n != chapter)
    orphans: list[dict[str, Any]] = []
    for entry in _all_ledger_quotes(store, snapshot):
        quote = entry["quote"]
        if quote in before_text and quote not in after_text and quote not in others:
            orphans.append(entry)
    return orphans


def verify_ledger(store: BookStore) -> dict[str, Any]:
    """账本自检：重放 events.jsonl 与 snapshot.json 逐字段比对，再做交叉校验。

    事件溯源的一致性保障：任何事件被删改、或 snapshot 被手工篡改，都会被重放对照发现。
    但"重放 vs 快照"对**重复入账**是盲的（两边都含重复项，必然一致），所以另加：
    每章至多一条 commit 事件；每条已入账章必须存在对应的 ch-NNNN.md 正文。
    另做两条只读的内容交叉校验（不计入 consistent，避免历史存量阻断流水线）：
    引文完整性（quote 必须能在正文里逐字找到）与身份卫生（重复 id / 重复结案）。
    只读，不修改任何文件；要修复用 `ledger repair --from-events`。
    """
    # 未初始化的项目必须先失败：空目录重放得到"零事件、零差异"，
    # 会让 `consistent: true` 变成假绿——而 verify 正是崩溃后第一个要跑的命令。
    store.read_head()
    replayed = replay_events(store)
    current = load_snapshot(store)
    fields = ("chapter", "entities", "debts", "hooks", "relations", "occupancy", "items", "conditions", "knowledge", "locations")
    diffs: list[str] = []
    for field in fields:
        if replayed.get(field) != current.get(field):
            diffs.append(field)
    extra_replayed = sorted(set(replayed.keys()) - set(fields))
    extra_current = sorted(set(current.keys()) - set(fields))
    if extra_replayed:
        diffs.append(f"extra_fields:replayed={extra_replayed}")
    if extra_current:
        diffs.append(f"extra_fields:snapshot={extra_current}")

    chapters = [c for c in event_chapters(store) if c > 0]
    seen: set[int] = set()
    duplicates: list[int] = []
    for ch in chapters:
        if ch in seen and ch not in duplicates:
            duplicates.append(ch)
        seen.add(ch)
    if duplicates:
        diffs.append(f"duplicate_chapter_events={sorted(duplicates)}")
    missing_files = sorted(ch for ch in seen if not store.chapter_md_path(ch).exists())
    if missing_files:
        diffs.append(f"missing_chapter_files={missing_files}")

    chain_issues = event_chain_issues(read_events(store))
    if chain_issues:
        for issue in chain_issues:
            diffs.append(f"event_chain:{issue['code']}@index={issue['index']}")

    quote_invalid = ledger_quote_issues(store, current)
    hygiene = ledger_hygiene_issues(store, current)
    return {
        "consistent": not diffs,
        "replayed_chapter": int(replayed.get("chapter") or 0),
        "snapshot_chapter": int(current.get("chapter") or 0),
        "replayed_entities": len(replayed.get("entities") or {}),
        "snapshot_entities": len(current.get("entities") or {}),
        "events": len(chapters),
        "duplicate_chapter_events": sorted(duplicates),
        "missing_chapter_files": missing_files,
        "diffs": diffs,
        "event_chain_ok": not chain_issues,
        "event_chain_issues": chain_issues,
        "quote_invalid": quote_invalid,
        "quote_consistent": not quote_invalid,
        "hygiene_issues": hygiene,
    }


def _apply_governance_event(snapshot: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    """Apply an append-only editorial ruling to the current derived state."""
    snap = dict(snapshot)
    chapter = int(event.get("effective_chapter") or 0)
    if chapter != int(snapshot.get("chapter") or 0):
        raise LedgerError(
            "governance_chapter_mismatch",
            "governance event must take effect at the latest committed chapter",
        )
    action = str(event.get("action") or "")
    if action == "canon.restamp":
        if not str(event.get("canon_sha") or "").strip():
            raise LedgerError("invalid_governance", "canon restamp requires canon_sha")
        return snap
    if action == "plan.spine_set":
        # State-less ruling (batch_select precedent): the spine text itself lives in the
        # plan truth store; the ledger only seals that the always-injected contract
        # changed, with a content digest for later audit.
        if not str(event.get("sha256_prefix") or "").strip():
            raise LedgerError("invalid_governance", "plan spine ruling requires sha256_prefix")
        return snap
    if action == "plan.chapter_patch":
        # State-less ruling (batch_select precedent): the patched beats live in the plan
        # truth store behind plan_shape_reject; the ledger only seals that an uncommitted
        # chapter's beats were surgically revised (plan-level deadlock repair channel).
        try:
            target = int(event.get("target_chapter"))
        except (TypeError, ValueError):
            raise LedgerError("invalid_governance", "plan chapter patch requires integer target_chapter")
        if target <= 0:
            raise LedgerError("invalid_governance", "plan chapter patch requires positive target_chapter")
        changed = event.get("changed_fields")
        if not isinstance(changed, list) or not changed or not all(str(f).strip() for f in changed):
            raise LedgerError("invalid_governance", "plan chapter patch requires changed_fields")
        unknown = [f for f in changed if str(f) not in ("beats", "location", "present")]
        if unknown:
            raise LedgerError("invalid_governance", f"plan chapter patch forbids fields: {unknown}")
        return snap
    if action == "plan.batch_select":
        # State-less ruling (canon.restamp precedent): the candidates archive lives
        # as a permanent editorial artifact; the ledger only seals the fact that a
        # selection happened, so replay never depends on the artifact file.
        for key in ("batch_from", "batch_to"):
            try:
                value = int(event.get(key))
            except (TypeError, ValueError):
                raise LedgerError("invalid_governance", f"plan batch selection requires integer {key}")
            if value <= 0:
                raise LedgerError("invalid_governance", f"plan batch selection requires positive {key}")
        if int(event.get("batch_to")) < int(event.get("batch_from")):
            raise LedgerError("invalid_governance", "batch_to must be >= batch_from")
        for key in ("selected_id", "candidates_path"):
            if not str(event.get(key) or "").strip():
                raise LedgerError("invalid_governance", f"plan batch selection requires {key}")
        losers = event.get("losers")
        if not isinstance(losers, list) or not losers or not all(
            isinstance(item, dict)
            and str(item.get("id") or "").strip()
            and str(item.get("why") or "").strip()
            for item in losers
        ):
            raise LedgerError("invalid_governance", "plan batch selection requires losers with id+why")
        return snap
    if action == "plan.rebudget":
        basis = event.get("basis")
        if not isinstance(basis, dict) or basis.get("schema") != "novel-ledger.chapter-rebudget.v1":
            raise LedgerError("invalid_governance", "plan rebudget requires a chapter-rebudget basis")
        for key in ("book_words", "chapter_words_target", "signed_total_chapters", "through_chapter", "written_words", "total_chapters"):
            value = basis.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise LedgerError("invalid_governance", f"plan rebudget requires positive integer {key}")
        additional = basis.get("additional_chapters")
        previous = event.get("previous_total_chapters")
        if isinstance(additional, bool) or not isinstance(additional, int) or additional < 0:
            raise LedgerError("invalid_governance", "plan rebudget requires nonnegative additional_chapters")
        if isinstance(previous, bool) or not isinstance(previous, int) or previous <= 0 or basis["total_chapters"] < previous:
            raise LedgerError("invalid_governance", "plan rebudget cannot reduce the previous total")
        if not isinstance(basis.get("continuation_volume"), str) or not basis["continuation_volume"].strip():
            raise LedgerError("invalid_governance", "plan rebudget requires a continuation volume")
        shifts = basis.get("milestone_shifts")
        if not isinstance(shifts, list):
            raise LedgerError("invalid_governance", "plan rebudget milestone_shifts must be a list")
        for shift in shifts:
            if not isinstance(shift, dict):
                raise LedgerError("invalid_governance", "plan rebudget milestone shift must be an object")
            before, after = shift.get("chapter_before"), shift.get("chapter_after")
            if (isinstance(before, bool) or not isinstance(before, int) or isinstance(after, bool)
                    or not isinstance(after, int) or before <= basis["through_chapter"] or after < before):
                raise LedgerError("invalid_governance", "plan rebudget may only move future milestones forward")
        return snap
    if action.startswith("hook."):
        hooks = [dict(h) for h in snapshot.get("hooks") or []]
        hook_id = str(event.get("hook_id") or "")
        hits = [i for i, h in enumerate(hooks) if h.get("id") == hook_id]
        if action in ("hook.close", "hook.defer"):
            if len(hits) != 1:
                raise LedgerError("hook_not_found", f"governance hook {hook_id!r} not found")
            hook = hooks[hits[0]]
            if action == "hook.close":
                hook["status"] = "closed"
                hook["close_reason"] = str(event.get("reason") or "")
            else:
                new_due = coerce_due(event.get("new_due"))
                if new_due is None:
                    raise LedgerError(
                        "invalid_new_due",
                        f"hook.defer new_due must be an integer chapter number; got {event.get('new_due')!r}",
                        {"hook_id": hook_id, "new_due": event.get("new_due")},
                    )
                hook["due"] = new_due
            hook["updated_chapter"] = chapter
        elif action == "hook.merge":
            into_id = str(event.get("into_id") or "")
            targets = [i for i, h in enumerate(hooks) if h.get("id") == into_id]
            if len(hits) != 1 or len(targets) != 1 or hook_id == into_id:
                raise LedgerError("hook_not_found", "merge source and target must be distinct existing hooks")
            source = hooks[hits[0]]
            target = hooks[targets[0]]
            if int(source.get("updated_chapter") or 0) > int(target.get("updated_chapter") or 0):
                merged = {**target, **source}
            else:
                merged = {**source, **target}
            merged["id"] = into_id
            merged["updated_chapter"] = chapter
            hooks[targets[0]] = merged
            hooks.pop(hits[0])
            # The id named by --into survives; the most recently updated values
            # survive, including a paid/closed lifecycle state on the source.
        else:
            raise LedgerError("invalid_governance", f"unknown action {action!r}")
        snap["hooks"] = hooks
        return snap
    if action.startswith("relation."):
        relations = [dict(r) for r in snapshot.get("relations") or []]
        if action == "relation.rename":
            from_name = str(event.get("from_name") or "")
            to_name = str(event.get("to_name") or "")
            if not from_name or not to_name or from_name == to_name:
                raise LedgerError("invalid_governance", "relation rename names must differ")
            if not any(r.get("who") == from_name or r.get("target") == from_name for r in relations):
                raise LedgerError("relation_not_found", f"relation name {from_name!r} not found")
            # A rename may collapse two variants with the same pair+kind. Use the
            # most recently updated entry's values, then mark the ruling's chapter.
            chosen: dict[tuple[str, str, str], tuple[dict[str, Any], int, int]] = {}
            for index, rel in enumerate(relations):
                previous_update = int(rel.get("updated_chapter") or 0)
                changed = False
                for field in ("who", "target"):
                    if rel.get(field) == from_name:
                        rel[field] = to_name
                        changed = True
                key = (str(rel.get("who") or ""), str(rel.get("target") or ""), str(rel.get("kind") or ""))
                prior = chosen.get(key)
                if prior is None or (previous_update, index) >= (prior[1], prior[2]):
                    winner = rel
                else:
                    winner = prior[0]
                if changed or prior is not None:
                    winner["updated_chapter"] = chapter
                chosen[key] = (winner, max(previous_update, prior[1] if prior else 0), index)
            snap["relations"] = [entry[0] for entry in chosen.values()]
        elif action == "relation.close":
            who = str(event.get("who") or "")
            target = str(event.get("target") or "")
            kind_substring = str(event.get("kind_substring") or "")
            hits = 0
            for rel in relations:
                if rel.get("who") != who or rel.get("target") != target:
                    continue
                if kind_substring and kind_substring not in str(rel.get("kind") or ""):
                    continue
                rel["status"] = "closed"
                rel["close_reason"] = str(event.get("reason") or "")
                rel["updated_chapter"] = chapter
                hits += 1
            if not hits:
                raise LedgerError("relation_not_found", f"relation {who!r}->{target!r} not found")
            snap["relations"] = relations
        else:
            raise LedgerError("invalid_governance", f"unknown action {action!r}")
        return snap
    raise LedgerError("invalid_governance", f"unknown action {action!r}")


def apply_event(snapshot: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    if event.get("type") == "governance":
        return _apply_governance_event(snapshot, event)
    snap = {
        "chapter": int(event.get("chapter") or snapshot.get("chapter") or 0),
        "entities": dict(snapshot.get("entities") or {}),
        "debts": list(snapshot.get("debts") or []),
        "hooks": list(snapshot.get("hooks") or []),
        "relations": list(snapshot.get("relations") or []),
        "occupancy": dict(snapshot.get("occupancy") or {}),
        "items": list(snapshot.get("items") or []),
        # conditions 必须从旧快照结转：漏带这一行会让每个事件都把历史角色状态清零，
        # 只剩当章新增——增量提交与重放两条路同病（千二百章验证的资产断言抓出）。
        "conditions": list(snapshot.get("conditions") or []),
        "knowledge": list(snapshot.get("knowledge") or []),
        # 地点簿（场景地图）同样必须结转：空间属性是章间最脆的连续性——worker 每章
        # 都是新会话，没有登记表，首章定下的楼层门牌到后章只能靠运气。
        "locations": list(snapshot.get("locations") or []),
    }
    delta = event.get("state_delta") or {}
    deaths = lifecycle_names(delta.get("deaths"), field="deaths")
    revivals = lifecycle_names(delta.get("revivals"), field="revivals")
    lifecycle_names(delta.get("nonliving"), field="nonliving")
    chapter = int(event.get("chapter") or 0)
    for move in delta.get("moves") or []:
        if not isinstance(move, dict):
            continue
        who = str(move.get("who") or "").strip()
        dest = str(move.get("to") or "").strip()
        if not who:
            continue
        ent = dict(snap["entities"].get(who) or {"id": who, "facts": []})
        ent["location"] = dest
        ent["updated_chapter"] = chapter
        snap["entities"][who] = ent
        # occupancy 在函数末尾统一重建一次，循环内不重建（O(moves×entities) → O(entities)）
    for fact in delta.get("facts") or []:
        if not isinstance(fact, dict):
            continue
        who = str(fact.get("who") or "").strip()
        text = str(fact.get("text") or "").strip()
        if not who or not text:
            continue
        ent = dict(snap["entities"].get(who) or {"id": who, "facts": [], "location": ""})
        facts = [f for f in (_norm_fact(x) for x in (ent.get("facts") or [])) if f]
        normalized = _norm_fact(fact)
        if normalized:
            facts.append(normalized)
        ent["facts"] = _evict_facts(facts)
        ent["updated_chapter"] = chapter
        snap["entities"][who] = ent
    knowledge = list(snap["knowledge"])
    knowledge_in_event: set[tuple[str, str]] = set()
    for update in delta.get("knowledge") or []:
        if not isinstance(update, dict):
            raise LedgerError("invalid_knowledge", "knowledge entries must be objects")
        if any(not isinstance(update.get(field), str) or not update[field].strip()
               for field in ("who", "topic_id", "claim", "source", "quote")):
            raise LedgerError("invalid_knowledge", "knowledge requires who/topic_id/claim/source/quote strings")
        if update.get("stance") not in {"knows", "believes", "suspects", "refuted"}:
            raise LedgerError("invalid_knowledge", "knowledge stance is invalid")
        who = str(update.get("who") or "").strip()
        topic_id = str(update.get("topic_id") or "").strip()
        if (who, topic_id) in knowledge_in_event:
            raise LedgerError("invalid_knowledge", f"duplicate knowledge update: {who}/{topic_id}")
        knowledge_in_event.add((who, topic_id))
        old_index = next(
            (i for i, item in enumerate(knowledge)
             if item.get("who") == who and item.get("topic_id") == topic_id),
            None,
        )
        item = dict(knowledge[old_index]) if old_index is not None else {}
        item.update(update)
        item["who"] = who
        item["topic_id"] = topic_id
        item.setdefault("first_recorded_chapter", chapter)
        item["updated_chapter"] = chapter
        if old_index is None:
            knowledge.append(item)
        else:
            knowledge[old_index] = item
    snap["knowledge"] = knowledge
    debts = list(snap["debts"])
    for debt in delta.get("debts") or []:
        if not isinstance(debt, dict):
            continue
        did = str(debt.get("id") or "").strip()
        if not did:
            continue
        debt = _normalize_due(debt, "debts")
        found = False
        for i, existing in enumerate(debts):
            if existing.get("id") == did:
                merged = dict(existing)
                merged.update({k: v for k, v in debt.items() if v is not None})
                merged["updated_chapter"] = chapter
                debts[i] = merged
                found = True
                break
        if not found:
            item = dict(debt)
            item["id"] = did
            item["updated_chapter"] = chapter
            debts.append(item)
    snap["debts"] = debts
    hooks = list(snap["hooks"])
    for hook in delta.get("hooks") or []:
        hid = str(hook.get("id") or "").strip()
        if not hid:
            continue
        hook = _normalize_due(hook, "hooks")
        found = False
        for i, existing in enumerate(hooks):
            if existing.get("id") == hid:
                merged = dict(existing)
                merged.update({k: v for k, v in hook.items() if v is not None})
                merged["updated_chapter"] = chapter
                hooks[i] = merged
                found = True
                break
        if not found:
            item = dict(hook)
            item["id"] = hid
            # 新建 hook 无 status 时默认 open：统一 audit（只认 open/active）
            # 与 select_hooks（只剔 paid/closed/abandoned）的口径——此前无 status 的
            # hook 在 pack 里被注入、在 audit 里却不算活跃，两处各说各话。
            item.setdefault("status", "open")
            # 长线伏笔后来会沿同一 id 更新 text/quote。首见证据单独留存，
            # 否则几百章后的快照只剩最新解释，策划无法核对回收是否公平。
            item["opened_chapter"] = chapter
            item["seed_text"] = str(hook.get("text") or "")
            if hook.get("quote"):
                item["seed_quote"] = hook["quote"]
            item["updated_chapter"] = chapter
            hooks.append(item)
    snap["hooks"] = hooks
    relations = list(snap["relations"])
    for rel in delta.get("relations") or []:
        who = str(rel.get("who") or "").strip()
        target = str(rel.get("target") or "").strip()
        kind = str(rel.get("kind") or "").strip()
        if not who or not target or not kind:
            continue
        merged = False
        for i, existing in enumerate(relations):
            if (
                existing.get("who") == who
                and existing.get("target") == target
                and existing.get("kind") == kind
            ):
                item = dict(existing)
                item.update({k: v for k, v in rel.items() if v is not None})
                item["updated_chapter"] = chapter
                relations[i] = item
                merged = True
                break
        if not merged:
            item = dict(rel)
            item["who"] = who
            item["target"] = target
            item["kind"] = kind
            item["status"] = str(item.get("status") or "open")
            item["updated_chapter"] = chapter
            relations.append(item)
        # `supersedes: true` = 本章这条是同一对角色关系的**演变/取代**，把该对角色其它
        # 还开着的条目置为 closed。没有这条机制时，关系以 (who, target, kind) 为键、kind
        # 是自由文本，同一段关系每章换一个说法就多一条 open 条目，永远收敛不了。
        if rel.get("supersedes"):
            superseded = str(rel.get("supersedes_kind") or "").strip()
            for i, existing in enumerate(relations):
                if existing.get("who") != who or existing.get("target") != target:
                    continue
                if existing.get("kind") == kind:
                    continue
                if superseded and str(existing.get("kind") or "") != superseded:
                    continue
                if relation_is_closed(existing):
                    continue
                retired = dict(existing)
                retired["status"] = "closed"
                retired["closed_chapter"] = chapter
                retired["closed_by_kind"] = kind
                relations[i] = retired
    snap["relations"] = relations
    for name in delta.get("new_names") or []:
        who = str(name).strip()
        if not who:
            continue
        ent = snap["entities"].setdefault(who, {"id": who, "facts": [], "location": "", "updated_chapter": chapter, "first_seen_chapter": chapter, "last_seen_chapter": chapter})
        if "first_seen_chapter" not in ent:
            ent["first_seen_chapter"] = chapter
        ent["last_seen_chapter"] = chapter
        ent["updated_chapter"] = chapter
        snap["entities"][who] = ent
    for name in delta.get("named") or []:
        who = str(name).strip()
        if not who:
            continue
        if who not in snap["entities"]:
            snap["entities"][who] = {"id": who, "facts": [], "location": "", "updated_chapter": chapter, "first_seen_chapter": chapter, "last_seen_chapter": chapter}
        else:
            ent = dict(snap["entities"][who])
            if "first_seen_chapter" not in ent:
                ent["first_seen_chapter"] = int(ent.get("updated_chapter") or chapter)
            ent["last_seen_chapter"] = chapter
            ent["updated_chapter"] = chapter
            snap["entities"][who] = ent
    for name in deaths:
        ent = dict(snap["entities"].get(name) or {"id": name, "facts": []})
        ent["dead"] = True
        ent["dead_chapter"] = chapter
        ent["updated_chapter"] = chapter
        ent["last_seen_chapter"] = chapter
        snap["entities"][name] = ent
    # 复活/夺舍/假死揭秘：`dead` 早前只能置位不能清除，于是"亡者归来"这类长篇主干
    # 在结构上做不了（只能回滚死亡那章，连带丢掉全部账本增量）。这里给出显式出口：
    # 声明 revivals 即撤销死亡标记并留痕，重放时同样确定。
    for name in revivals:
        if name not in snap["entities"]:
            continue
        ent = dict(snap["entities"][name])
        ent["dead"] = False
        ent["revived_chapter"] = chapter
        ent["updated_chapter"] = chapter
        snap["entities"][name] = ent
    items = list(snap.get("items") or [])
    for item_delta in delta.get("items") or []:
        if not isinstance(item_delta, dict):
            continue
        explicit_id = str(item_delta.get("id") or "").strip()
        iname = str(item_delta.get("name") or "").strip()
        iid = explicit_id or iname
        if not iid:
            continue
        found = False
        for i, existing in enumerate(items):
            eid = str(existing.get("id") or "").strip()
            ename = str(existing.get("name") or "").strip()
            # 同一件东西的判定：显式 id 相同，或（在无法用 id 区分时）名字相同。
            # 早前只按 `id == iid` 匹配，而 iid 是 `id or name` 的兜底——同一柄剑
            # 第一次带 id、第二次只给 name 就会被当成两件，账本里长出幽灵第二条。
            if explicit_id and eid == explicit_id:
                pass
            elif iname and ename == iname and not (explicit_id and eid):
                pass
            else:
                continue
            merged = dict(existing)
            merged.update({k: v for k, v in item_delta.items() if v is not None})
            old_holder = str(existing.get("holder") or "").strip()
            new_holder = str(item_delta.get("holder") or "").strip()
            if (
                str(item_delta.get("status") or "").strip().lower() == "transferred"
                and old_holder
                and new_holder
                and new_holder != old_holder
            ):
                # transferred 是本章动作；快照记录转交后的当前在持状态。
                merged["status"] = "held"
            merged["updated_chapter"] = chapter
            items[i] = merged
            found = True
            break
        if not found:
            new_item = dict(item_delta)
            new_item["id"] = iid
            if "name" not in new_item:
                new_item["name"] = iid
            new_item["status"] = str(new_item.get("status") or "held")
            new_item["updated_chapter"] = chapter
            items.append(new_item)
    snap["items"] = items
    # 角色状态（伤势/体力/欠债/职务…）：按 (who, kind, text) 合并，状态可迁移。
    # 这是"不可逆代价"的落点——正典里写得最硬的部分（断肢、毁容、资格吊销、破产清零）
    # 此前没有任何结构模型，单章看不出来，是长篇最常见的崩盘点之一。
    conditions = [_norm_condition(c) for c in (snap.get("conditions") or [])]
    by_key = {_condition_key(c): c for c in conditions if c}
    for raw in delta.get("conditions") or []:
        cond = _norm_condition(raw)
        if not cond:
            continue
        key = _condition_key(cond)
        # 同键更新视为最新状态；移到末尾后才参加每人的额度淘汰。
        merged = dict(by_key.pop(key, {}))
        merged.update({k: v for k, v in cond.items() if v is not None})
        by_key[key] = merged
    snap["conditions"] = _evict_conditions(list(by_key.values()))
    _apply_location_delta(snap, delta.get("locations"), chapter)
    _reindex_occupancy(snap)
    return snap


# —— 地点簿（场景地图）——
# 空间属性的注册表：id/name/aliases + attributes{键: {value, chapter, quote}}。
# 职责边界：账本只做确定性的登记与结转；「正文与注册表冲突」是提交闸的判断
# （gates.location_attribute_conflict，replaces 显式豁免），不是 apply 的判断——
# 有意的翻修搬迁靠 replaces 留痕，无意的漂移靠闸门拦下。
_CN_DIGITS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "百": 100, "零": 0}


def _cn_segment_to_int(segment: str) -> int | None:
    """中文数字段 → int（十/百进位）：四→4、十→10、十二→12、二十→20、一百零一→101。"""
    total, current = 0, 0
    for c in segment:
        v = _CN_DIGITS[c]
        if v >= 10:
            current = (current or 1) * v
            total += current
            current = 0
        else:
            current = current * 10 + v
    return total + current


def norm_spatial_value(value: Any) -> str:
    """空间属性值的比较口径：去全部空白、小写、中文数字段归一为阿拉伯（「四楼」与「4楼」同值）。"""
    text = "".join(str(value or "").split())
    if not text:
        return ""
    out: list[str] = []
    i = 0
    while i < len(text):
        if text[i] in _CN_DIGITS:
            j = i
            while j < len(text) and text[j] in _CN_DIGITS:
                j += 1
            number = _cn_segment_to_int(text[i:j])
            out.append(str(number) if number is not None else text[i:j])
            i = j
        else:
            out.append(text[i].lower())
            i += 1
    return "".join(out)


def _norm_location(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise LedgerError("invalid_location", "locations entries must be objects")
    name = str(raw.get("name") or "").strip()
    ident = str(raw.get("id") or "").strip() or name
    if not ident:
        raise LedgerError("invalid_location", "location requires id or name")
    aliases = [str(a).strip() for a in (raw.get("aliases") or []) if str(a).strip() and str(a).strip() != name]
    attributes: dict[str, str] = {}
    raw_attrs = raw.get("attributes") or {}
    if not isinstance(raw_attrs, dict):
        raise LedgerError("invalid_location", "location attributes must be an object of key→value")
    for key, value in raw_attrs.items():
        key_s = str(key).strip()
        value_s = str(value or "").strip()
        if not key_s:
            raise LedgerError("invalid_location", "location attribute keys must be non-empty")
        if not value_s:
            raise LedgerError("invalid_location", f"location attribute {key_s} must be non-empty")
        attributes[key_s] = value_s
    quote = str(raw.get("quote") or "").strip()
    if attributes and not quote:
        raise LedgerError("invalid_location", "location declarations with attributes require a verbatim quote")
    status = str(raw.get("status") or "open").strip().lower()
    if status not in {"open", "closed"}:
        raise LedgerError("invalid_location", "location status must be open|closed")
    replaces = [str(k).strip() for k in (raw.get("replaces") or []) if str(k).strip()]
    if not attributes and not aliases and not replaces and status == "open" and not raw.get("status"):
        raise LedgerError("invalid_location", "location entry carries nothing to register (attributes/aliases/status)")
    return {"id": ident, "name": name, "aliases": aliases, "attributes": attributes,
            "quote": quote, "status": status, "replaces": replaces}


def _location_names(loc: dict[str, Any]) -> list[str]:
    names = [str(loc.get("id") or ""), str(loc.get("name") or "")]
    names.extend(str(a) for a in (loc.get("aliases") or []))
    return [n for n in names if n]


def _apply_location_delta(snap: dict[str, Any], raw_entries: Any, chapter: int) -> None:
    if not raw_entries:
        return
    locations = [l if isinstance(l, dict) else {} for l in (snap.get("locations") or [])]
    index: dict[str, dict[str, Any]] = {}
    for loc in locations:
        for n in _location_names(loc):
            index.setdefault(n, loc)
    for raw in raw_entries:
        entry = _norm_location(raw)
        target: dict[str, Any] | None = None
        for n in [entry["id"], entry["name"], *entry["aliases"]]:
            if n and n in index:
                target = index[n]
                break
        if target is None:
            target = {
                "id": entry["id"],
                "name": entry["name"] or entry["id"],
                "aliases": [],
                "attributes": {},
                "established_chapter": chapter,
                "status": "open",
            }
            locations.append(target)
        # 名称与别名互通：同地异名在后章合并到同一条，旧名保留为别名。
        if entry["name"] and entry["name"] != target.get("name"):
            old_name = str(target.get("name") or "")
            merged = list(dict.fromkeys([old_name, *(target.get("aliases") or []), entry["name"]]))
            target["name"] = entry["name"]
            target["aliases"] = [a for a in merged if a and a != entry["name"]]
        if entry["aliases"]:
            target["aliases"] = [a for a in dict.fromkeys([*(target.get("aliases") or []), *entry["aliases"]]) if a != target.get("name")]
        for key, value in entry["attributes"].items():
            target.setdefault("attributes", {})[key] = {"value": value, "chapter": chapter, "quote": entry["quote"]}
        if raw.get("status"):
            target["status"] = entry["status"]
        target["updated_chapter"] = chapter
        for n in _location_names(target):
            index[n] = target
    snap["locations"] = locations


def _reindex_occupancy(snap: dict[str, Any]) -> None:
    occ: dict[str, list[str]] = {}
    for name, ent in (snap.get("entities") or {}).items():
        loc = str((ent or {}).get("location") or "").strip()
        if not loc:
            continue
        occ.setdefault(loc, []).append(name)
    # 同一地点的在场人按名排序：snapshot.json 以 canonical_json（sort_keys）落盘，实体
    # 顺序是字典序；重放却按事件创建序展开实体。occupancy 列表若保插入序，两者在
    # 「两个具名角色同处一地」时就必然不等，verify_ledger 会误报 occupancy 分歧
    # （实体字典本身的比较不敏感顺序，只有这份派生列表会泄漏顺序差）。
    snap["occupancy"] = {loc: sorted(names) for loc, names in occ.items()}


def apply_delta_conflicts(snapshot: dict[str, Any], delta: dict[str, Any]) -> list[dict[str, Any]]:
    """死者在场/移动停线；显式非活人在场或合法复活可放行。

    pack 一直告诉写者「除非章拍/正典明确安排其作为回忆、遗物、鬼魂等非活人存在」，
    但 delta 里没有任何通道能声张这件事——写者照指示写了闪回，装配一把名字填进 `named`
    就撞上 `blocked`，只能人工介入。`nonliving` 就是那个缺失的通道：显式、随事件留痕、可重放。
    只放宽不收紧——不声明时行为与从前完全一致。
    """
    issues: list[dict[str, Any]] = []
    entities = snapshot.get("entities") or {}
    nonliving = set(lifecycle_names(delta.get("nonliving"), field="nonliving"))
    revival_names = lifecycle_names(delta.get("revivals"), field="revivals")
    revivals = set(revival_names)
    deaths = set(lifecycle_names(delta.get("deaths"), field="deaths"))
    for who in revival_names:
        # A chapter may depict death followed by revival. Without either a
        # recorded death or this chapter's death, a revival would invent state.
        if not (entities.get(who) or {}).get("dead") and who not in deaths:
            issues.append({"code": "revival_not_dead", "who": who})
    exempt = nonliving | revivals
    for move in delta.get("moves") or []:
        who = str(move.get("who") or "").strip()
        if not who or who in exempt:
            continue
        ent = entities.get(who) or {}
        if ent.get("dead"):
            issues.append({"code": "dead_speaking", "who": who})
    for name in delta.get("named") or []:
        who = str(name).strip()
        if who in exempt:
            continue
        ent = entities.get(who) or {}
        if ent.get("dead"):
            issues.append({"code": "dead_speaking", "who": who})
    return issues


def commit_event(store: BookStore, chapter: int, state_delta: dict[str, Any], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    snapshot = load_snapshot(store)
    existing_events = read_events(store)
    # Check replay before lifecycle conflicts: a partially committed death is
    # already visible in the snapshot, but belongs to this same accepted output.
    # The pipeline decides whether that existing event is resumable.
    existing_max = max(
        (int(ev.get("chapter") or 0) for ev in existing_events if ev.get("type") != "governance"),
        default=0,
    )
    if existing_max >= int(chapter):
        raise LedgerError(
            "ledger_replay_conflict",
            f"ledger already holds chapter {existing_max}; refusing to append chapter {chapter} twice",
            {
                "chapter": int(chapter), "max_event_chapter": existing_max,
                "hint": "run ledger verify, then rollback via retry-authorize (reopen) or ledger repair --from-events",
            },
        )
    conflicts = apply_delta_conflicts(snapshot, state_delta)
    if conflicts:
        raise LedgerError("ledger_conflict", "delta conflicts with ledger", conflicts)
    # A merge permanently retires the source id. A later chapter that reuses it
    # would silently resurrect the duplicate after the governance ruling.
    retired = {
        str(ev.get("hook_id") or ""): str(ev.get("into_id") or "")
        for ev in existing_events
        if ev.get("type") == "governance" and ev.get("action") == "hook.merge"
    }
    reused = sorted({
        str(h.get("id") or "") for h in state_delta.get("hooks") or []
        if isinstance(h, dict) and str(h.get("id") or "") in retired
    })
    if reused:
        raise LedgerError(
            "retired_hook_id",
            "chapter delta references hook ids retired by governance merge",
            {"ids": reused, "surviving_ids": {old: retired[old] for old in reused}},
        )
    retired_names = {
        str(ev.get("from_name") or ""): str(ev.get("to_name") or "")
        for ev in existing_events
        if ev.get("type") == "governance" and ev.get("action") == "relation.rename"
    }
    reused_names = sorted({
        str(rel.get(field) or "")
        for rel in state_delta.get("relations") or [] if isinstance(rel, dict)
        for field in ("who", "target")
        if str(rel.get(field) or "") in retired_names
    })
    if reused_names:
        raise LedgerError(
            "retired_relation_name",
            "chapter delta references relation names retired by governance rename",
            {"names": reused_names, "canonical_names": {old: retired_names[old] for old in reused_names}},
        )
    event = {
        "chapter": chapter,
        "ts": now_ts(),
        "state_delta": state_delta,
    }
    if extra:
        event.update(extra)
    # 事件哈希链封印：真源每行可验证、防篡改（见 event_chain_issues 注释）。
    prev_hash = str(existing_events[-1].get("hash") or "") if existing_events else ""
    if prev_hash:
        event["prev_hash"] = prev_hash
    event["hash"] = _event_digest(event)
    # 先计算并验证派生视图，确保 delta 结构无误再写真源与派生视图，防止中途异常污染 events.jsonl
    snap = apply_event(snapshot, event)
    from ..infra.util import append_bytes
    append_bytes(store.events_path, canonical_json(event))
    atomic_json(store.snapshot_path, snap)
    return snap


def append_governance_event(
    store: BookStore,
    *,
    action: str,
    actor: str,
    reason: str,
    fields: dict[str, Any],
) -> dict[str, Any]:
    """Append a ruling and update the snapshot without altering past chapter events.

    The caller holds the project's write lock. A crash after appending but before
    snapshot replacement is recoverable with ``ledger repair --from-events``.
    """
    store.read_head()
    actor = str(actor or "").strip()
    reason = str(reason or "").strip()
    if not actor or not reason:
        raise LedgerError("invalid_args", "governance actor and reason are required")
    if any(key in fields for key in (
        "type", "chapter", "effective_chapter", "hash", "prev_hash", "action", "actor", "reason", "ts"
    )):
        raise LedgerError("invalid_governance", "governance fields contain reserved keys")
    events = read_events(store)
    chain_issues = event_chain_issues(events)
    if chain_issues:
        raise LedgerError("event_chain_broken", "refusing governance on a broken event chain", chain_issues)
    snapshot = load_snapshot(store)
    if replay_events(store) != snapshot:
        raise LedgerError(
            "ledger_snapshot_drift",
            "snapshot differs from events; run ledger verify and repair before governance",
        )
    event: dict[str, Any] = {
        "type": "governance",
        "action": action,
        "actor": actor,
        "reason": reason,
        "effective_chapter": int(snapshot.get("chapter") or 0),
        "ts": now_ts(),
        **fields,
    }
    if events:
        event["prev_hash"] = str(events[-1].get("hash") or "")
    event["hash"] = _event_digest(event)
    updated = apply_event(snapshot, event)  # validate target before writing anything
    from ..infra.util import append_bytes
    append_bytes(store.events_path, canonical_json(event))
    atomic_json(store.snapshot_path, updated)
    return event


def render_now_card(
    store: BookStore,
    *,
    protagonist: str,
    chapter_plan: dict[str, Any],
    volume_spine: str,
    cap_chars: int = 800,
    snapshot: dict[str, Any] | None = None,
    fact_focus: str = "",
) -> dict[str, Any]:
    name = (protagonist or "").strip()
    if not name:
        raise LedgerError("missing_now_card", "protagonist is required to render the NOW card")
    snap = snapshot if snapshot is not None else load_snapshot(store)
    ent = dict((snap.get("entities") or {}).get(name) or {})
    location = str(chapter_plan.get("location") or ent.get("location") or "").strip()
    wound = str(chapter_plan.get("wound") or "").strip()
    owes = _owes_for(name, snap.get("debts") or [])
    speech = list(chapter_plan.get("speech") or [])[:3]
    facts = facts_for_pack(ent.get("facts"), cap=3, focus=fact_focus, who=name)
    card = {
        "name": name,
        "goal": str(chapter_plan.get("goal") or volume_spine or "").strip(),
        "wound": wound,
        "speech": [str(s) for s in speech if str(s).strip()],
        "location": location,
        "owes": owes,
        "facts": facts,
        "dead": bool(ent.get("dead")),
    }
    if card["dead"]:
        raise LedgerError("missing_now_card", "protagonist is marked dead; cannot begin")
    rendered = (
        f"{card['name']}。目标：{card['goal'] or '（未写）'}。"
        f"所在：{card['location'] or '（未知）'}。"
        f"伤口：{card['wound'] or '（未记）'}。"
        f"欠谁：{card['owes'] or '（无）'}。"
    )
    card["rendered"] = rendered
    return card


def _owes_for(name: str, debts: list[dict[str, Any]]) -> str:
    bits = []
    for debt in sorted(debts, key=_updated_chapter, reverse=True):
        if not _debt_active(debt):
            continue
        who = str(debt.get("who") or "")
        if who == name or name in str(debt.get("text") or ""):
            bits.append(str(debt.get("text") or debt.get("id") or "").strip())
        if len(bits) >= 3:
            break
    return "；".join(x for x in bits if x)


def state_near(
    store: BookStore,
    *,
    pinned_names: list[str],
    pinned_cap: int,
    recent_cap: int,
    occupancy_locations: list[str] | None = None,
    occupancy_per_location: int = 12,
    snapshot: dict[str, Any] | None = None,
    fact_focus: str = "",
) -> dict[str, Any]:
    snap = snapshot if snapshot is not None else load_snapshot(store)
    entities = snap.get("entities") or {}
    pinned: list[dict[str, Any]] = []
    seen: set[str] = set()
    for name in pinned_names:
        if not name or name in seen:
            continue
        seen.add(name)
        ent = entities.get(name)
        if not ent:
            pinned.append({"id": name, "location": "", "facts": [], "missing": True})
        else:
            pinned.append(_entity_view(name, ent, fact_focus=fact_focus))
        if len(pinned) >= pinned_cap:
            break
    ranked = sorted(
        entities.items(),
        key=lambda kv: int((kv[1] or {}).get("updated_chapter") or 0),
        reverse=True,
    )
    recent: list[dict[str, Any]] = []
    for name, ent in ranked:
        if name in seen:
            continue
        recent.append(_entity_view(name, ent, fact_focus=fact_focus))
        if len(recent) >= recent_cap:
            break
    debts = sorted(
        (d for d in (snap.get("debts") or []) if _debt_active(d)),
        key=_updated_chapter,
        reverse=True,
    )
    # 占用表必须按本章相关地点切片：整库 occupancy 会随全书实体数线性膨胀（百万字书
    # 数千命名实体 → 每 pack 多注入上万字符）。只保留本章地点 + 在场者所在地，且每地封顶。
    full_occ = snap.get("occupancy") or {}
    occ: dict[str, list[str]] = {}
    if occupancy_locations:
        for loc in occupancy_locations:
            if not loc:
                continue
            names = full_occ.get(loc) or []
            occ[loc] = list(names)[: max(1, int(occupancy_per_location))]
    return {
        "pinned": pinned,
        "recent": recent,
        "occupancy": occ,
        "open_debt_ids": [d.get("id") for d in debts[:8] if d.get("id")],
    }


def _ranked_active_relations(
    snap: dict[str, Any], names: list[str]
) -> list[tuple[int, int, int, dict[str, Any], str, str]]:
    """在场双方优先，再按更新章倒序；供所有角色关系视图复用。"""
    name_set = {str(n).strip() for n in names if str(n).strip()}
    if not name_set:
        return []
    candidates: list[tuple[int, int, int, dict[str, Any], str, str]] = []
    for idx, rel in enumerate(snap.get("relations") or []):
        if not isinstance(rel, dict):
            continue
        if relation_is_closed(rel):
            continue
        who = str(rel.get("who") or "")
        target = str(rel.get("target") or "")
        if who not in name_set and target not in name_set:
            continue
        try:
            updated = int(rel.get("updated_chapter") or 0)
        except (TypeError, ValueError):
            updated = 0
        candidates.append((int(who in name_set and target in name_set), updated, idx, rel, who, target))
    candidates.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return candidates


def select_relations(
    store: BookStore,
    *,
    names: list[str],
    cap: int = 6,
    snapshot: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """关系索引切片：涉及在场者的有效关系封顶 cap 条，在场双方优先。"""
    snap = snapshot if snapshot is not None else load_snapshot(store)
    candidates = _ranked_active_relations(snap, names)
    keep = max(0, int(cap))
    out: list[dict[str, Any]] = []
    for _both, updated, _idx, rel, who, target in candidates[:keep]:
        entry = {
            "who": who,
            "target": target,
            "kind": rel.get("kind") or "",
            "status": rel.get("status") or "open",
        }
        if updated:
            entry["updated_chapter"] = updated
        out.append(entry)
    # 被丢弃的是**最旧**的关系（排序为最近更新优先）——往往正是写者最该记住的长线纽带。
    # 必须回传条数，否则写者拿着一个有界视野却以为它是全集。
    return out, max(0, len(candidates) - keep)


def _entity_view(name: str, ent: dict[str, Any], *, fact_focus: str = "") -> dict[str, Any]:
    return {
        "id": name,
        "location": ent.get("location") or "",
        "facts": facts_for_pack(ent.get("facts"), cap=3, focus=fact_focus, who=name),
        "dead": bool(ent.get("dead")),
        "updated_chapter": int(ent.get("updated_chapter") or 0),
    }


def select_debts(
    store: BookStore,
    *,
    location: str,
    present: list[str],
    cap: int,
    snapshot: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    snap = snapshot if snapshot is not None else load_snapshot(store)
    present_set = set(present)
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for debt in snap.get("debts") or []:
        if not _debt_active(debt):
            continue
        score = 0
        text = str(debt.get("text") or "")
        who = str(debt.get("who") or "")
        loc = str(debt.get("location") or "")
        if who in present_set:
            score += 5
        if location and (loc == location or location in text):
            score += 4
        if any(p and p in text for p in present_set):
            score += 2
        if debt.get("due"):
            score += 3
        if score <= 0:
            continue
        scored.append((score, _updated_chapter(debt), debt))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    keep = max(0, int(cap))
    out = []
    for _, _, debt in scored[:keep]:
        item = {
            "id": debt.get("id"),
            "who": debt.get("who") or "",
            "text": debt.get("text") or "",
            "status": debt.get("status") or "open",
        }
        if debt.get("due") not in (None, ""):
            item["due"] = str(debt.get("due"))
        out.append(item)
    return out, max(0, len(scored) - keep)


def select_hooks(
    store: BookStore,
    *,
    chapter: int,
    cap: int = 5,
    snapshot: dict[str, Any] | None = None,
    priority_ids: list[str] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """伏笔/钩子账本切片：优先注入距离当前章最近的即时活跃钩子（近期逾期与临近到期），
    逾期过久（>30章）的远古钩子大幅降权，防止旧时代废弃伏笔霸占注入位导致世界观幻觉。
    """
    snap = snapshot if snapshot is not None else load_snapshot(store)
    hooks = snap.get("hooks") or []
    scored: list[tuple[int, int, dict[str, Any]]] = []
    priority_order = {str(hid): index for index, hid in enumerate(priority_ids or []) if hid}
    for hook in hooks:
        if str(hook.get("status") or "open") in ("paid", "closed", "abandoned"):
            continue
        due = coerce_due(hook.get("due")) or 0
        if due <= 0:
            score = 0  # 无到期日：排在最后
        elif due <= chapter:
            overdue_dist = chapter - due
            if overdue_dist > 30:
                # 逾期超过30章的远古旧钩子：大幅降权，防止污染后续世界观
                score = max(10, 100 - overdue_dist)
            else:
                # 近期逾期（0~30章内）：高优先级催促兑现，越近期越靠前
                score = 1000 - overdue_dist * 15
        else:
            # 即将到期（未来章）：越临近当前章越高
            dist_to_due = due - chapter
            score = 800 - min(dist_to_due * 10, 700)
        # 本章章拍已点名的旧伏笔必须先入包：远期 due 不应挤掉当前要显形的原始证据。
        ident = str(hook.get("id"))
        priority = len(priority_order) - priority_order[ident] if ident in priority_order else 0
        scored.append((priority, score, hook))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    keep = max(0, int(cap))
    out = []
    for _, _, hook in scored[:keep]:
        item = {
            "id": hook.get("id"),
            "text": hook.get("text") or "",
            "status": hook.get("status") or "open",
        }
        if hook.get("due") not in (None, ""):
            item["due"] = hook.get("due")
        out.append(item)
    return out, max(0, len(scored) - keep)


def select_knowledge(
    *,
    snapshot: dict[str, Any],
    names: list[str],
    topic_ids: list[str] | None = None,
    cap: int = 16,
) -> tuple[list[dict[str, Any]], int]:
    """只切在场人物的个人认知；本章点名的话题先于近期记录。"""
    name_set = {str(name).strip() for name in names if str(name).strip()}
    refs = list(dict.fromkeys(str(topic).strip() for topic in (topic_ids or []) if str(topic).strip()))
    priority = {topic: len(refs) - index for index, topic in enumerate(refs)}
    candidates: list[tuple[int, int, int, dict[str, Any]]] = []
    for index, item in enumerate(snapshot.get("knowledge") or []):
        if not isinstance(item, dict) or str(item.get("who") or "") not in name_set:
            continue
        topic = str(item.get("topic_id") or "")
        if not topic:
            continue
        candidates.append((priority.get(topic, 0), _updated_chapter(item), index, item))
    candidates.sort(key=lambda row: (row[0], row[1], row[2]), reverse=True)
    keep = max(0, int(cap))
    chosen_indexes: list[int] = []
    # 每个显式引用的话题先选一条；否则第一个话题被许多在场人共享时，
    # 可能把后续被点名的话题完全挤出有界切片。
    for topic in refs:
        if len(chosen_indexes) >= keep:
            break
        first = next((index for index, row in enumerate(candidates) if row[3].get("topic_id") == topic), None)
        if first is not None:
            chosen_indexes.append(first)
    chosen_set = set(chosen_indexes)
    chosen_indexes.extend(index for index in range(len(candidates)) if index not in chosen_set)
    selected = [dict(candidates[index][3]) for index in chosen_indexes[:keep]]
    return selected, max(0, len(candidates) - len(selected))


def get_character_continuity_records(
    snapshot: dict[str, Any],
    protagonist: str,
    present_names: list[str],
    current_chapter: int,
    fact_focus: str = "",
    selected_relations: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """提取在场人物与主角之间的历史认知和连续性档案。

    用于在 pack 中显式注入防失忆（Anti-Amnesia）、防时间倒带与防设定冲突约束。
    全部措辞为通用概念，不绑定任何具体人设/题材。
    """
    entities = snapshot.get("entities") or {}
    # Pack 装配时复用唯一的有界关系切片；单独调用也只看同口径的前 6 条。
    relations = selected_relations if selected_relations is not None else [
        row[3] for row in _ranked_active_relations(snapshot, present_names)[:6]
    ]
    out: list[dict[str, Any]] = []

    # 1. 首先锚定主角自身的核心状态（以账本 facts 为准，不做题材假设）
    if protagonist and protagonist in entities:
        p_ent = entities.get(protagonist) or {}
        p_facts = p_ent.get("facts") or []
        p_record = {
            "name": protagonist,
            "is_protagonist": True,
            "location": p_ent.get("location") or "",
            "facts": facts_for_pack(p_facts, cap=3, focus=fact_focus, who=protagonist),
            "continuity_directive": (
                f"【主角状态铁律】：主角【{protagonist}】的一切言行必须与其历史事实一致，"
                "严禁失忆、时间倒带、动作重复或与已记设定/事实冲突。"
            ),
        }
        out.append(p_record)

    # 2. 锚定在场其他配角与主角的关系
    for name in present_names:
        name = str(name).strip()
        if not name or name == protagonist:
            continue
        ent = entities.get(name) or {}
        first_seen = int(ent.get("first_seen_chapter") or ent.get("updated_chapter") or 0)
        last_seen = int(ent.get("last_seen_chapter") or ent.get("updated_chapter") or 0)
        facts = facts_for_pack(ent.get("facts"), cap=3, focus=fact_focus, who=name)
        
        # 查找该人物与主角之间的关系
        rel_kinds = []
        for r in relations:
            if relation_is_closed(r):
                continue
            who = str(r.get("who") or "")
            target = str(r.get("target") or "")
            if (who == name and target == protagonist) or (who == protagonist and target == name):
                kind = str(r.get("kind") or "")
                if kind:
                    rel_kinds.append(kind)
        
        is_first_appearance = (first_seen == 0 or first_seen >= current_chapter)
        
        record: dict[str, Any] = {
            "name": name,
            "is_first_appearance": is_first_appearance,
            "first_seen_chapter": first_seen if first_seen > 0 else None,
            "last_seen_chapter": last_seen if last_seen > 0 else None,
            "relations_with_protagonist": rel_kinds,
            "facts": facts,
            "dead": bool(ent.get("dead")),
        }
        
        if bool(ent.get("dead")):
            record["continuity_directive"] = (
                f"【死亡警告】角色【{name}】在账本中已标记死亡："
                "除非本章章拍/项目正典明确安排其作为回忆、遗物、鬼魂等非活人存在，"
                "否则严禁让其说话、行动或以活人身份参与本章。"
            )
        elif not is_first_appearance:
            rel_desc = f"【{'/'.join(rel_kinds)}】" if rel_kinds else "【已有既往交集】"
            record["continuity_directive"] = (
                f"【重大连续性纪律】：角色【{name}】已于第 {first_seen} 章登场，"
                f"与主角关系为 {rel_desc}，双方已知晓彼此！"
                f"正文中绝对禁止写成‘素未谋面/初次相见/询问姓名/陌生人’，必须继承既往动机、情绪与恩怨！"
            )
        else:
            record["continuity_directive"] = f"角色【{name}】为首次出场的新人物。"
            
        out.append(record)
        
    return out


def select_items(
    store: BookStore,
    *,
    names: list[str],
    cap: int = 8,
    snapshot: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """筛选在场人物当前持有的活跃物品，优先注入最近更新的条目。"""
    snap = snapshot if snapshot is not None else load_snapshot(store)
    name_set = {str(n).strip() for n in names if str(n).strip()}
    if not name_set:
        return [], 0
    candidates: list[tuple[int, int, dict[str, Any], str, str]] = []
    for index, item in enumerate(snap.get("items") or []):
        if not isinstance(item, dict):
            continue
        holder = str(item.get("holder") or "").strip()
        status = str(item.get("status") or "held").strip().lower()
        # 已不在手上的状态不注入；有新持有人的转交已在 apply_event 归一为 held。
        # `used`/`quantity:0` 的消耗品也不能继续被当成在持资产交给写者。
        # 未登记的未知状态仍照常注入：宁可多给，也不要静默藏掉写者可能需要的道具。
        if status in _ITEM_NOT_HELD_STATUSES:
            continue
        if isinstance(item.get("quantity"), int) and item["quantity"] <= 0:
            continue
        if holder not in name_set:
            continue
        try:
            updated_chapter = int(item.get("updated_chapter") or 0)
        except (TypeError, ValueError):
            updated_chapter = 0
        candidates.append((updated_chapter, index, item, holder, status))
    # 同章更新时后登记的条目优先；旧快照没有 updated_chapter 时也保持确定性。
    candidates.sort(key=lambda row: (row[0], row[1]), reverse=True)
    out: list[dict[str, Any]] = []
    for _, _, item, holder, status in candidates[: max(0, cap)]:
        entry = {
            "id": str(item.get("id") or ""),
            "name": str(item.get("name") or ""),
            "holder": holder,
            "quantity": item.get("quantity", 1),
            "kind": str(item.get("kind") or "misc"),
            "status": status,
        }
        if item.get("desc"):
            entry["desc"] = str(item["desc"])
        out.append(entry)
    return out, len(candidates) - len(out)
