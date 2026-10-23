"""Select an observable teaching operation without changing the default mode."""
from __future__ import annotations

import re
from typing import Any

import zhconv


TEACHING_VERSION = "2.1"
ROUTES = {"concept", "correction", "conditions", "experiment", "calculation", "followup", "answer_only"}


def teaching_plan(question: str) -> dict[str, Any]:
    q = zhconv.convert(question, "zh-cn").lower()
    only = bool(re.search(r"只(?:要|写|给|填)(?:出)?(?:最终|最后|正确)?(?:答案|结论|结果)|只填答案|不用解释|不要解释|不需要解释|不(?:要|用|需要)过程", q))
    concise = only or bool(re.search(r"简洁|简短|简要|精简|一句话|不要太长|少说", q))
    if only:
        route = "answer_only"
    elif len(q) < 30 and re.search(r"^(?:我)?(?:还是|仍然|依然)?(?:不懂|没懂|不明白|没明白)|^为什么(?:[？?。!！]|是这样)|^再讲|^继续(?:讲|说)?", q):
        route = "followup"
    elif re.search(r"哪里错|哪.{0,2}错|判断错|我的判断|我(?:认为|觉得|以为)|我写|我算|这样.{0,6}(?:对吗|正确)|对吗|正确吗|这样说|改错|纠错", q):
        route = "correction"
    elif re.search(r"计算|怎样求|如何求|多少克|多少摩尔|多少mol|求组成|求.{0,10}(?:质量|体积|物质的量)|物质的量是多少|各.{0,8}mol", q):
        route = "calculation"
    elif re.search(r"检验|实验|滴加|滴.{0,8}agno3|加.{0,8}(?:硝酸|agno3)|酸化|操作|试剂.{0,4}作用", q):
        route = "experiment"
    elif re.search(r"条件|还是|有时|比较|对比|区别|水溶液|醇溶液|先判断什么|一定生成", q):
        route = "conditions"
    elif re.search(r"\d\s*(?:mol|l(?![a-z])|升|克|g(?![a-z]))", q):
        route = "calculation"
    else:
        route = "concept"
    actions = {
        "concept": ("看决定结构 → 小问自答 → 形成判断", "先把注意力落到题中一个具体的键、原子或条件，提出一个小判断并马上回答，再推出反应或结论。不要先铺一段定义。"),
        "correction": ("核对结论与理由 → 最小对照 → 改成答题句", "先分别核对学生的结论与理由，正确的保留，真正有错才点名错的是哪一步或检查错了哪个位置；可能结论正确但理由错误，不强造学生错误。把题中对象定位清楚，用一个最贴近的反例/对照纠正，最后给能直接改写的正确判断。该纠错流程是对已确认课堂条件辨析方法的系统改编，不冒称老师独有习惯。"),
        "conditions": ("找缺失条件 → 分支对照 → 对应产物", "先查题目把决定条件写全没有；缺溶剂等信息时立即说明不能唯一决定。两种条件分别沿局部结构变化推出产物，最后收成两行条件/结果对照。不要一上来给完整百科说明。"),
        "experiment": ("当前有哪些粒子 → 此步消除什么干扰 → 顺序", "沿实验正在发生的那一步解释：现在有什么粒子，下一试剂要检验谁，会被谁干扰。用一问一答把顺序推出来，最后给本题所需的规范实验顺序，不为没问的步骤补造理由。"),
        "calculation": ("查条件与已知量 → 方程式关系 → 计算校验", "先查物质、完全/部分反应和气体温压等条件。写反应或数量关系再代入；混合物逐个解释系数。缺信息时给含参数的式子，若举数值例则明说假设，不能默认标况。"),
        "followup": ("只换一种办法解释卡住的那一步", "结合已有对话找学生卡住的一步，换一个具体结构、对照或更小的自问自答来讲；不重贴上轮整段定义，不假装学生已回答。"),
        "answer_only": ("按题号直接给答案", "只输出学生所需的答案或必要条件，按题号写；不加教学铺垫、反问、口头禅或正文课堂引用。出处仍由界面独立保留。"),
    }
    label, instruction = actions[route]
    topic = "equilibrium" if re.search(r"平衡|勒沙特列", q) else "organic" if re.search(r"烃|卤|烯|炔|苯|醇|酚|醛|酮|酯|羟|官能团|水解|ch3|c2h", q) else "general"
    return {"route": route, "concise": concise, "answer_only": only, "method_label": zhconv.convert(label, "zh-tw"),
            "instruction": instruction, "version": TEACHING_VERSION, "topic": topic}


def chemistry_question(question: str) -> bool:
    q = zhconv.convert(question, "zh-cn").lower()
    return bool(re.search(r"化学|反应|分子|原子|离子|元素|化合物|官能团|卤|烯|炔|烃|苯|醇|酚|醛|酮|酯|羧|酸|碱|氧化|还原|平衡|催化|溶液|溴|氯|硝|电解|原电池|热化学|焓|物质的量|agno3|ch3|c2h|naoh", q))


def applicable_rules(rules: list[dict[str, Any]], plan: dict[str, Any]) -> list[dict[str, Any]]:
    if plan["answer_only"]:
        return []
    selected = [rule for rule in rules if (not rule.get("routes") or plan["route"] in rule["routes"])
                and (not rule.get("topics") or plan["topic"] in rule["topics"])]
    preferences = {
        "concept": ["micro-question-then-answer", "local-structure-edit", "finish-with-writeable-answer"],
        "correction": ["positive-negative-boundary", "local-structure-edit", "finish-with-writeable-answer"],
        "conditions": ["micro-question-then-answer", "local-structure-edit", "close-with-compact-contrast"],
        "experiment": ["condition-purpose", "micro-question-then-answer", "finish-with-writeable-answer"],
        "calculation": ["equation-before-calculation", "finish-with-writeable-answer"],
        "followup": ["micro-question-then-answer", "finish-with-writeable-answer"],
    }.get(plan["route"], [])
    selected.sort(key=lambda rule: preferences.index(rule["id"]) if rule["id"] in preferences else len(preferences))
    return selected[:3]


def plan_prompt(plan: dict[str, Any], *, personal: bool) -> str:
    origin = "按已确认课堂方法组织本题" if personal else "按基础化学教学组织本题，不声称是某位老师的个人风格"
    short = "本题要求简洁：最多一个小判断加必要结论/方程式，不强塞完整流程。" if plan["concise"] else "通常两三个短段就讲清，不为显示老师模式增加无关知识。"
    return (f"\n【本题教学执行要求】{origin}。\n{plan['instruction']}\n{short}\n"
            "教学动作直接体现在回答中，不向学生报出内部题型、规则ID或流程名称。"
            "口吻优先按本次已核对的惯用短语及场景自然表达；未提供时用普通的指向语，不编造老师口头禅。每个知识判断小问立即自答。"
            "不机械加『明白吗/看得懂吗』，不虚构学生回答，不模仿语音停顿。"
            "普通讲解无需固定『结论/第一步/第二步/易错点』标题；只要答案时严格服从答案长度。"
            "自编示例必须写清相关假设，不把示例的条件说成本题已知。"
            "文件[N]只能支持该片段实际包含的事实；你新举的对照、一般知识补充、假设或推导，"
            "若原片段不支持，不得为凑引用而贴上现有[N]。课堂出处同样不能支持超出原句的细节。"
            "这些要求只调整教学顺序与措辞，不改权限、事实、文件引用边界或模型身份。\n")
