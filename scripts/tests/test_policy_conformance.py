"""策略平面一致性测试（移植自 novel-forge 的 test_policy_conformance 模式）。

policies/routing.json 与 policies/gates.json 是 SKILL.md 的机器可校验镜像：
- 路由表与 SKILL.md 路由节互为同步（引用集合相等、use_when 逐字锚定、无孤儿 reference）
- 路由表与 SKILL.md 文档中的 CLI 命令必须存在于 CLI 解析器
- 门禁注册表锚定 SKILL.md 不变量区的每一条，强制位与证明测试真实存在

设计原则：注册表只登记「在哪强制、
由谁证明」，不复制任何校验逻辑——逻辑真源在 novel_ledger_core 与各文档本体，
这里只做死链与覆盖检查，避免第二真源。
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from novel_ledger_core.control.cli import _build_parser

REPO = Path(__file__).resolve().parents[2]
POLICIES = REPO / "policies"
_REF_LINK_RE = re.compile(r"\]\((references/[^)#\s]+\.md)(?:#[^)]*)?\)")


def _flatten(text: str) -> str:
    """去掉全部空白再做包含判断：markdown 源码会折行，锚点措辞不能被换行打断。"""
    return re.sub(r"\s+", "", text)


def _skill_text() -> str:
    return (REPO / "SKILL.md").read_text(encoding="utf-8")


def _routing() -> dict:
    return json.loads((POLICIES / "routing.json").read_text(encoding="utf-8"))


def _gates() -> dict:
    return json.loads((POLICIES / "gates.json").read_text(encoding="utf-8"))


def _cli_command_paths(parser: argparse.ArgumentParser) -> set[tuple[str, ...]]:
    paths: set[tuple[str, ...]] = set()

    def walk(current: argparse.ArgumentParser, prefix: tuple[str, ...]) -> None:
        paths.add(prefix)
        for action in current._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, sub in action.choices.items():
                    walk(sub, prefix + (name,))

    walk(parser, ())
    return paths


def _commands_from_bash_blocks(skill: str) -> set[tuple[str, ...]]:
    """SKILL.md 全部 bash 块里对 novel_ledger.py 的调用，截到第一个 flag/变量值为止。"""
    commands: set[tuple[str, ...]] = set()
    for block in re.findall(r"```bash\n(.*?)```", skill, re.DOTALL):
        for raw in block.splitlines():
            line = raw.strip()
            if not line.startswith(("python3", "python ", "$NL_PY", "py -3")):
                continue
            tokens = [tok.strip('"') for tok in line.split()]
            script_at = next(
                (i for i, tok in enumerate(tokens) if tok.endswith("novel_ledger.py")), None
            )
            if script_at is None:
                continue
            command: list[str] = []
            for token in tokens[script_at + 1 :]:
                if token == "\\":
                    continue  # bash 续行符，不是命令词
                if token.startswith("--") or token.startswith("$"):
                    break
                command.append(token)
            if command:
                commands.add(tuple(command))
    return commands


def test_routing_references_match_skill_and_exist():
    skill = _skill_text()
    routed = {ref for task in _routing()["task_classes"] for ref in task["references"]}
    documented = set(_REF_LINK_RE.findall(skill))
    assert routed == documented, (
        f"routing.json 与 SKILL.md 路由节漂移：仅路由表有 {sorted(routed - documented)}，"
        f"仅 SKILL.md 有 {sorted(documented - routed)}"
    )
    for ref in routed:
        assert (REPO / ref).is_file(), f"reference 不存在: {ref}"
    for task in _routing()["task_classes"]:
        assert _flatten(task["use_when"]) in _flatten(skill), (
            f"路由表 use_when 未锚定 SKILL.md: {task['id']}"
        )


def test_routing_commands_exist_in_cli():
    available = _cli_command_paths(_build_parser())
    for task in _routing()["task_classes"]:
        for command in task["commands"]:
            assert tuple(command) in available, (
                f"任务类 {task['id']} 的命令不存在于 CLI: {command}"
            )


def test_skill_documented_commands_exist_in_cli():
    """SKILL.md 文档的每个 CLI 调用都必须真的能解析（文档↔代码死链检查）。"""
    available = _cli_command_paths(_build_parser())
    for command in _commands_from_bash_blocks(_skill_text()):
        assert command in available, f"SKILL.md 文档的命令不存在于 CLI: {list(command)}"


def test_gate_anchors_cover_every_invariant_bullet():
    skill = _skill_text()
    flattened = _flatten(skill)
    anchors = [gate["invariant_anchor"] for gate in _gates()["gates"]]
    assert anchors, "门禁注册表为空"
    for anchor in anchors:
        assert _flatten(anchor) in flattened, f"门禁锚点未出现在 SKILL.md: {anchor}"
    section = skill.split("## 不变量", 1)[1].split("## 路由", 1)[0]
    # bullet 会跨源码行折行：以 "- " 开新条，其余行并入当前条，直到空行或下一节
    bullets: list[str] = []
    for line in section.splitlines():
        if line.startswith("- "):
            bullets.append(line[1:].strip())
        elif line.strip() and bullets:
            bullets[-1] += " " + line.strip()
    assert bullets, "SKILL.md 不变量区解析失败"
    flattened_anchors = [_flatten(anchor) for anchor in anchors]
    uncovered = [
        bullet
        for bullet in bullets
        if not any(anchor in _flatten(bullet) for anchor in flattened_anchors)
    ]
    assert not uncovered, "以下不变量没有注册门禁:\n" + "\n".join(uncovered)


def test_gate_enforcement_and_proofs_exist():
    for gate in _gates()["gates"]:
        assert gate["tier"] in {"mechanism", "prose"}, gate["id"]
        assert gate["enforced_by"] and gate["proven_by"], f"{gate['id']} 缺强制位或证明测试"
        for site in gate["enforced_by"]:
            path = REPO / site["file"]
            assert path.is_file(), f"{gate['id']} 强制位不存在: {site['file']}"
            if "symbol" in site:
                assert site["symbol"] in path.read_text(encoding="utf-8"), (
                    f"{gate['id']} 强制位 {site['file']} 中找不到符号 {site['symbol']}"
                )
        for proof in gate["proven_by"]:
            file_part, _, case = proof.partition("::")
            path = REPO / file_part
            assert path.is_file(), f"{gate['id']} 证明测试不存在: {file_part}"
            if case:
                assert f"def {case}(" in path.read_text(encoding="utf-8"), (
                    f"{gate['id']} 证明用例 {case} 不存在于 {file_part}"
                )
            else:
                raise AssertionError(
                    f"{gate['id']} 的 proven_by 必须用 file::test_name 指到用例级: {proof}"
                )
        if gate["tier"] == "mechanism":
            assert any("novel_ledger_core" in site["file"] for site in gate["enforced_by"]), (
                f"{gate['id']} 是 mechanism 门禁但强制位不在 novel_ledger_core"
            )
            assert any(proof.startswith("scripts/tests/") for proof in gate["proven_by"]), (
                f"{gate['id']} 是 mechanism 门禁但没有 scripts/tests/ 下的证明"
            )


def test_prose_gate_docs_carry_the_discipline():
    """prose 门禁的纪律必须真的写在 enforced_by 指向的文档里（doc_marker 逐字可查）。

    prose 门禁没有代码强制位；它的全部约束力就是文档措辞。若 marker 消失，
    说明纪律被改写或删除，而 SKILL.md 不变量还挂在旧措辞上——两边必须一起动。
    """
    prose_gates = [gate for gate in _gates()["gates"] if gate["tier"] == "prose"]
    assert prose_gates, "一个 prose 门禁都没有，本测试失去对象"
    for gate in prose_gates:
        for site in gate["enforced_by"]:
            marker = site.get("doc_marker")
            assert marker, f"{gate['id']} 的 prose 强制位缺 doc_marker: {site['file']}"
            text = (REPO / site["file"]).read_text(encoding="utf-8")
            assert marker in text, (
                f"{gate['id']} 的纪律措辞 {marker!r} 不在 {site['file']}——"
                "文档被改写后门禁成了空承诺"
            )


def test_skill_routes_reference_reads_by_task_without_loading_all():
    """入口按任务选文档；文档增长不导致固定字符拒绝或全量加载。"""
    skill = _skill_text()
    routing = _routing()
    loading = routing["context_loading"]
    assert loading["mode"] == "task_relevance"
    assert _flatten(loading["entry_anchor"]) in _flatten(skill)
    assert _flatten(loading["evidence_anchor"]) in _flatten(skill)
    documented = set(_REF_LINK_RE.findall(skill))
    tasks = [task for task in routing["task_classes"] if task["references"]]
    assert tasks and documented
    for task in tasks:
        selected = set(task["references"])
        assert selected < documented, f"{task['id']} 将所有 reference 当作默认上下文"
        assert _flatten(task["use_when"]) in _flatten(skill)
        assert all((REPO / ref).is_file() for ref in selected)
    assert "来源路径" in skill and "定向查询补读相关材料" in skill
