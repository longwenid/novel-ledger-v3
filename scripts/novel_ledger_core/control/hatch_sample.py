"""hatch 合法样例的唯一真源：校验器同源、CLI 可导出、测试直接锁定。

契约文档刻意不复制第二份大 JSON（防漂移），代价曾转嫁给每个新宿主——
跨环境开书要花二十分钟读校验器与测试源码反推 manifest 形状。本模块把
测试夹具升为包内真源：`book hatch --sample-manifest` 导出它，
test_choice_hatch 用它跑全量校验——样例失效即测试红，零漂移由构造保证。
"""

from __future__ import annotations

from typing import Any


def _decision(decision_id: str) -> dict[str, Any]:
    return {
        "id": decision_id,
        "axis": f"{decision_id} 的唯一决策轴",
        "cardinality": "one",
        "status": "confirmed",
        "source": "candidate",
        "custom_allowed": True,
        "none_allowed": decision_id in {"core_relationships", "story_mechanism"},
        "options": [
            {"id": "A", "label": "稳健方案", "outcome": "形成明确故事承诺", "benefit": "兑现路径清晰", "cost": "需维护因果链", "risk": "节奏可能偏克制"},
            {"id": "B", "label": "激进方案", "outcome": "以高反转密度推进", "benefit": "短期吸引力强", "cost": "伏笔成本更高", "risk": "容易透支后续空间"},
        ],
        "selected": ["A"],
        "summary": "采用稳健方案并纳入最终开书合同",
        "recommended_id": "A",
        "recommendation_reason": "更符合当前题材与长篇连载规模",
    }


def _voice_decision() -> dict[str, Any]:
    decision = _decision("voice_style")
    decision["axis"] = "全书叙述文风"
    decision["voice_id"] = "shijing"
    decision["options"] = [
        {"id": "A", "voice_id": "shijing", "label": "市井烟火", "outcome": "具体事务与人情推进叙述", "benefit": "生活质感清楚", "cost": "需写实地组织场面", "risk": "处理不当会琐碎"},
        {"id": "B", "voice_id": "cinematic", "label": "电影镜头", "outcome": "可见行动与空间推进叙述", "benefit": "场面感明确", "cost": "需精确调度视点", "risk": "处理不当会只剩画面"},
    ]
    decision["summary"] = "选择市井烟火作为全书叙述文风"
    decision["recommendation_reason"] = "适合作为未指定文风时的默认推荐"
    return decision


def _extra_decision(slug: str, after: str) -> dict[str, Any]:
    decision = _decision(f"extra_{slug}")
    decision["axis"] = f"{slug} 的独立决策轴"
    decision["after"] = after
    decision["reason"] = "现有基础门无法单独确定这项开书取舍"
    return decision


def add_volume_outline_fixture(manifest: dict[str, Any], *, count: int = 3) -> dict[str, Any]:
    """Authored, distinct whole-book outlines shared by hatch-based regressions."""
    from novel_ledger_core.infra.scale import expected_total_chapters
    outlines = [
        "第一卷以药草失窃和矿场灭口案建立韩立的生存困境。他原本只希望攒够药钱救治母亲，却在一次交货时发现配额账目的重量和实际药草数量对不上。墨大夫要求他查清差额，戒律堂则以查禁私藏为由封住山道。韩立不能直接相信任何一方，只能借辨药和记账的手艺将采药记录、出库签名和工钱欠条互相核对。每取得一项证据都须付出可见的生活代价：错过交货失去工钱，转移药圃耗去原本留给母亲的药材，使用掌天瓶又留下能够被追踪的异香。南宫婉因追查禁地异常进入矿场，她的目标与韩立的脱身愿望部分一致，双方交换半份证据而不交换全部底牌。合作的关键不在轻易建立友情，而在让对方能够查验自己的说法。戒律堂利用工钱和家属威胁矿工作证，主角需要决定只带家人逃走，还是冒险保住不能自行离开的证人。卷中每次小规模胜利都会改变搜查方向，迫使主角放弃原本安全的路径。他最终借配额结算的时间差让执行者的假账暴露，却只能证明矿场并非孤立的黑账，不能在首卷就宣布资源制度已经终止。卷终韩立带着完整的残页和证人脱离矿场，但原有药圃、收入和安稳身份均被破坏。南宫婉以宗门总账中的相同编号确认残页的去向，两人形成受条件约束的同盟。第二卷因此必须处理证据进入宗门后如何取得公信力的问题。人物线在本卷完成从只保护自身到愿意承担有限责任的选择，母亲的治疗和矿工的安全仍构成持续压力，而不是被脱身胜利一笔带过。旧执行者的惩罚只结算矿场局部事件，配额制度背后的利益链仍待追索。",
        "第二卷承接矿场残页、幸存证人与南宫婉的有限同盟，把私人逃亡转成能够进入宗门质证程序的调查。韩立带来的账目并不足以自行洗清罪名，天星宗可以声称残页来自被驱逐的矿工，南宫婉也不能凭身份替证据背书。两人首先通过坊市交易找到配额编号对应的灵植流向，发现同一批药材在不同账册中被赋予了相互矛盾的用途。主角需要在维持母亲治疗、支付证人生活费用和保住追查线索之间分配有限资源，不能靠突然获得无穷灵石解除困境。南宫婉坚持维护宗门自主，反对把一宗的罪责直接扩大到所有修炼者，因此双方在公开证据的时机上发生真实分歧。韩立倾向提前暴露局部假账换取喘息，她则主张等到总账能够互证后再行动。戒律堂不再单纯搜杀，而是安排一份能够解释残页的伪造账本，并诱使证人在有利于执行者的条件下承认错误。调查过程须让读者看到双方如何核实证词、比对出入库时间和寻找可以被第三方重复检查的凭据。主角因保护证人错过一次修炼配额，意识到自主选择并不等于每次选择都能获利。卷末他和南宫婉冒着各自失去身份保障的风险取得真实总账，把矿场记录、坊市交易和宗门分配连接起来。他们获得公开质证资格，但这份资格有明确截止条件，对手开始推动三宗年度总账合并，试图把献祭链永久洗白。本卷结算的是残页作为可信证据的地位，以及同盟能否共同承担公开的代价，不替下一卷完成全书制度胜利。下一卷将以年度盟会为压力场，主角必须让证据转为足以改变制度运转的裁决。母亲与矿工的处境随公开调查改善有限，却因利益集团反击面临新的风险，人物关系在分歧后的相互承担中获得可信推进。",
        "第三卷从公开质证资格和完整证据链出发，进入三宗年度盟会与资源分配制度的最后对抗。韩立已经能够证明局部账目被篡改，却仍须回答制度执行者以稀缺资源稳定秩序为名提出的现实问题。宗门并非所有成员都支持献祭，也并非制度一经揭露就会自行停止。主角要区分直接获利的责任人、被迫协助的执行者和失去配额就无法生存的普通修炼者，避免用惩罚一个反派冒充解决全书矛盾。南宫婉维护宗门自主的目标与公开改革并不天然一致，双方需要在保留必要分配与终止强制献祭之间形成能够落实的替代安排。年度封账持续收紧时限，对手通过转移原账、施压证人和制造资源短缺争取拖延。主角不能以新增能力回滚已付的寿元或重造失去的药圃，必须用此前取得的互证材料和可信关系补足程序条件。卷中应逐一回收残页编号、药材去向、证人选择和掌天瓶异香所留下的后果，让公开裁决依靠已经铺陈的证据，而非结尾才出现的万能证人。韩立最终放弃能够独占稀缺配额的交易，将能够验证的账目交给多方保存，并承担自己从中获益的部分责任。公开裁决需要真实改变配额运转、停止献祭链并给受害矿工留下可追踪的补偿途径，不能只宣布众人感动或主角成为最强者。南宫婉通过维护新程序而获得宗门自主的实际保障，双方的互信在目标仍各自独立的情况下成立。母亲的治疗有明确结果，主角可以决定继续修炼或回到生活，而不再因身份被强迫进入旧分配制度。终局以制度终止和自主选择权的可见状态完成主线，保留新秩序需要日常维护的现实余波。最后的平静场景应带着此前不可逆代价，不把伤亡、失去的资源和关系裂痕抹去；开放的生活去向不等于核心承诺尚未兑现。",
    ]
    first = manifest["step5_volume1"]
    remaining = manifest["book_words"] - first["word_budget"]
    budgets = [first["word_budget"]] if count == 1 else [first["word_budget"], remaining // 2, remaining - remaining // 2]
    volumes = []
    for index, budget in enumerate(budgets, 1):
        volumes.append({"volume": index, "title": first["title"] if index == 1 else ("宗门总账" if index == 2 else "年度盟会"),
                        "spine": first["spine"] if index == 1 else ("取得公开质证资格" if index == 2 else "终止献祭配额制度"),
                        "goal": (first["spine"] if index == 1 else "把已有证据转为公开制度裁决"),
                        "word_budget": budget, "chapters_budget": expected_total_chapters(budget, 3200),
                        "outline": outlines[index - 1], "plot_role": ("建立证据与有限责任" if index == 1 else "推进公信力" if index == 2 else "兑现自主选择权"),
                        "inherits": ("承接主角家计与矿场身份" if index == 1 else "承接前卷留下的证据、代价与同盟"),
                        "advances": ("矿场求生并形成有限互信" if index == 1 else "公开证据与制度对抗迫使人物共同承担代价"),
                        "ending_direction": "本卷局部结果构成终止资源垄断的必要因果环节",
                        "next_handoff": first["next_volume_hook"] if index == 1 else "带公开质证资格进入年度盟会" if index == 2 else "核心承诺兑现后留下新秩序的生活余波"})
    manifest["volume_outline_contract"] = {"schema": "novel-ledger.volume-outlines.v1", "volume_count": count, "volumes": volumes}
    if count == 1:
        for act in manifest["step4_outline"]["acts"]:
            act["volumes"] = "1"
    return manifest


def build_sample_manifest() -> dict[str, Any]:
    """合法的 guided hatch.v3 样例（校验器同源，改动须经全量测试）。"""
    decision_ids = (
        "reader_promise", "plot_architecture", "protagonist_engine", "core_relationships",
        "world_opposition", "story_mechanism", "main_arc", "scale", "voice_style",
    )
    manifest = {
        "schema": "novel-ledger.hatch.v3",
        "title": "大道长生",
        "protagonist": "韩立",
        "book_words": 2_000_000,
        "voice": "shijing",
        "wizard": {
            "mode": "guided", "status": "confirmed", "final_confirmation": True,
            "decisions": [_voice_decision() if item == "voice_style" else _decision(item) for item in decision_ids],
        },
        "step1_hook": {
            "concept": "底层采药童子在资源垄断的修真界，以严苛等价代价换取生机。",
            "genre_tags": ["凡人流", "仙侠修真", "智斗"],
            "target_audience": "偏好严谨守恒与步步为营的长篇读者",
            "selling_points": "资源严格守恒，每一次突破都带来可追踪代价",
            "narrative_tone": "克制、写实、紧张",
            "plot_architecture": "因果阶梯式成长与调查并行",
            "protagonist_archetype": "谨慎务实的生存型行动者",
        },
        "step2_protagonist": {
            "name": "韩立", "initial_status": "偏远矿场的贫苦采药童子",
            "desire_surface": "脱离矿奴身份并救治母亲", "desire_deep": "取得不被制度支配的自主选择权",
            "core_pressure": "一次轻信曾令同伴受伤，此后难以交付信任", "persona": "谨慎、务实、谋定后动",
            "limitation": "过度预设恶意，容易错失真实盟友", "skillset": "草药辨识、账目核算与环境陷阱",
        },
        "relationship_structure": {
            "mode": "present", "pattern": "互相制衡的双人同盟",
            "relationships": [{
                "name": "韩立与南宫婉的风险同盟", "participants": ["韩立", "南宫婉"],
                "relationship_type": "利益合作逐步转为互信", "independent_goal": "南宫婉要维护宗门自主并追查禁地异常",
                "boundary": "双方不得替对方放弃独立目标", "change_arc": "试探—交换证据—共同承担代价",
            }],
        },
        "story_mechanism": {
            "mode": "present", "name": "掌天瓶", "mechanism": "吸纳月华凝练绿液并催熟灵植",
            "cost_and_backlash": "使用会折损精气并产生可追踪异香", "limitations": "每日仅能凝聚一滴且不能作用于金石",
            "workaround": "以隔绝气息的地下药圃分批使用",
        },
        "material_life": {"mode": "none"},
        "long_arc_question": {"mode": "none"},
        "main_plotline": {
            "mode": "present", "core_quest": "打破修炼资源垄断并取得自由修行资格",
            "driving_force": "保护家人与兑现对矿工的承诺", "volume1_hook": "从矿场药草失窃与灭口案切入",
            "endgame_condition": "垄断制度终止且主角取得自主选择权",
            "progression_arc": [
                {"stage": "矿场求生", "goal": "保住证据并脱离矿奴身份"},
                {"stage": "宗门破局", "goal": "公开资源制度的献祭链条"},
            ],
        },
        "step3_world": {
            "iron_rules": ["灵气与寿元守恒，突破和秘法都存在不可逆代价", "宗门垄断筑基资源，配额流转必须留下账目"],
            "power_system": "凡俗武学—练气—筑基；每层都有容量和寿元上限",
            "economy": "金银与灵石双轨流通，坊市交易受宗门配额约束",
            "opposition": {
                "name": "天星宗配额制度", "kind": "制度与执行者共同构成的对抗",
                "interest": "维持稀缺资源的稳定分配与宗门控制", "conflict": "主角的药方和证据会打破配额合法性",
                "first_pressure": "戒律堂封锁矿场并搜查所有药童",
            },
            "allies": "墨大夫与矿工组成目标不同、互相戒备的临时合作网",
        },
        "step4_outline": {
            "structure_mode": "四幕多卷因果阶梯",
            "acts": [
                {"name": "矿场破局", "volumes": "1", "arc": "求生到掌握证据", "stakes": "矿工生死"},
                {"name": "宗门追索", "volumes": "2-3", "arc": "逃亡到制度对抗", "stakes": "配额秩序"},
            ],
            "event_spine": {
                "schema": "novel-ledger.event-spine.v1",
                "mainline": {
                    "id": "thread-main", "name": "资源垄断证据链",
                    "purpose": "推动主角从求生走向制度对抗", "mainline_link": "直接承载全书核心使命",
                    "open_stage": "矿场破局", "payoff_stage": "宗门追索", "participants": ["韩立", "戒律堂"],
                    "events": [
                        {"id": "main-ledger-fragment", "stage": "矿场破局", "event": "主角取得配额账本残页", "change": "从被追捕者变为证据持有者"},
                        {"id": "main-ledger-proof", "stage": "宗门追索", "event": "残页与宗门总账完成互证", "change": "私人逃亡升级为公开制度对抗"},
                    ],
                },
                "subplots": [{
                    "id": "thread-trust", "name": "风险同盟互信",
                    "purpose": "检验主角过度戒备的性格缺陷", "mainline_link": "同盟提供总账入口但也带来暴露风险",
                    "open_stage": "矿场破局", "payoff_stage": "宗门追索", "participants": ["韩立", "南宫婉"],
                    "events": [
                        {"id": "subplot-trust-exchange", "stage": "矿场破局", "event": "双方交换可相互验证的半份证据", "change": "建立有限合作"},
                        {"id": "subplot-trust-cost", "stage": "宗门追索", "event": "双方共同承担公开证据的代价", "change": "利益同盟转为可信伙伴"},
                    ],
                }],
                "timelines": [
                {
                    "id": "clock-protagonist", "kind": "protagonist", "name": "主角生存窗口", "start_state": "封锁前尚有三日转移证据",
                    "events": [
                        {"id": "timeline-protagonist-search", "order": 1, "stage": "矿场破局", "event": "搜查范围收紧", "deadline": "封矿前", "consequence": "证据与家人同时暴露"},
                        {"id": "timeline-protagonist-hearing", "order": 2, "stage": "宗门追索", "event": "取得公开质证资格", "deadline": "宗门会审前", "consequence": "只能继续逃亡"},
                    ],
                },
                {
                    "id": "clock-antagonist", "kind": "antagonist", "name": "戒律堂灭口进度", "start_state": "执行者正在比对药童名册",
                    "events": [
                        {"id": "timeline-antagonist-list", "order": 1, "stage": "矿场破局", "event": "锁定账本接触者", "deadline": "封矿当夜", "consequence": "矿工证人被逐个清除"},
                        {"id": "timeline-antagonist-forgery", "order": 2, "stage": "宗门追索", "event": "伪造总账自证", "deadline": "会审开场", "consequence": "主角证据失去公信力"},
                    ],
                },
                {
                    "id": "clock-world", "kind": "world", "name": "宗门配额结算", "start_state": "本季配额即将封账",
                    "events": [
                        {"id": "timeline-world-close", "order": 1, "stage": "矿场破局", "event": "矿场提交本季假账", "deadline": "月末封账", "consequence": "残页成为孤证"},
                        {"id": "timeline-world-merge", "order": 2, "stage": "宗门追索", "event": "三宗合并年度总账", "deadline": "年度盟会", "consequence": "献祭链被永久洗白"},
                    ],
                },
                ],
                "tension_curve": [
                    {"stage": "矿场破局", "level": 3, "mode": "build", "pressure": "封矿搜查持续收紧", "turn": "主角发现账本残页", "payoff": "以账目漏洞脱身", "next_imbalance": "残页指向宗门总账"},
                    {"stage": "宗门追索", "level": 5, "mode": "climax", "pressure": "三宗联合追索证据", "turn": "同盟取得总账原本", "payoff": "公开献祭链", "next_imbalance": "旧秩序崩解后的权力真空"},
                ],
            },
            "milestones": [{"chapter": 3, "kind": "turning_point", "description": "第一次保住完整证据链"}],
            "long_term_commitments": [],
        },
        "step5_volume1": {
            "title": "七玄风云", "spine": "从濒死药童到带着证据脱离矿场",
            "word_budget": 180_000, "chapters_budget": 57, "climax": "利用配额账目反制戒律堂执行者",
            "next_volume_hook": "证据指向越国三宗的共同配额账本",
        },
        "step6_beats": {
            "chapters": [
                {
                    "chapter": chapter, "title": title,
                    "beats": [
                        {"id": f"c{chapter}b1", "text": f"{anchor}出现并打破原有安全判断", "must": anchor},
                        {"id": f"c{chapter}b2", "text": f"主角围绕{anchor}取得一条可验证证据", "must": anchor},
                        {"id": f"c{chapter}b3", "text": f"主角因{anchor}付出代价并进入下一困境", "must": anchor},
                        {"id": f"c{chapter}b4", "text": f"{anchor}的余波落到旁人身上，带出一条口信或物件", "must": anchor},
                        {"id": f"c{chapter}b5", "text": f"主角对{anchor}定下明日之约，留一个未完", "must": anchor},
                    ],
                }
                for chapter, title, anchor in ((1, "采药少年", "悬崖落石"), (2, "绿液异变", "药草催熟"), (3, "账目反制", "配额账本"))
            ]
        },
        "quant_items": [{"key": "配额", "value": "每月十份", "rule": "统一对账", "banned": "随意变更"}],
        "glossary": {"小绿瓶": "掌天瓶"},
        "hard_constraints_mode": "present",
        "hard_constraints": ["能力必有代价", "任何关键反转必须具备前置证据"],
    }
    return add_volume_outline_fixture(manifest)
