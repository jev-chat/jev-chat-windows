"""Fixed Jev question set. Instructions/criteria in English; chat text stays Chinese."""

from __future__ import annotations

import re

JUDGE_QUESTIONS: dict = {
    "literal_question": {
        "type": "noul",
        "instructions": (
            "Is the other person's latest message meant purely literally, with no subtext? "
            "Judge from the whole thread, not one sentence in isolation."
        ),
        "criteria": {
            "true": (
                "The latest message is a straightforward statement, question, or plan "
                "with no implied accusation, test, sarcasm, hint, or unsaid request."
            ),
            "false": (
                "There is subtext: a test of whether you remember or care, sarcasm, "
                "an implied complaint, a hint they will not say outright, a trap question, "
                "an accusation dressed as a question, or a cold/short line that really means blame."
            ),
        },
    },
    "true_intent": {
        "type": "choice",
        "instructions": (
            "What is the other person's true intent in the latest message, given the full conversation? "
            "Prefer tone and context over surface wording. "
            "If they explicitly say they feel physically unwell, are in pain, exhausted, or emotionally overwhelmed, "
            "and are looking for care or a check-in, choose seek_care rather than casual_chat. "
            "If they are checking whether you remember something or still care, choose confirm_you_care "
            "even if the words look like a request to 'say it' or to do something. "
            "If they already accepted and closed the matter peacefully, choose close_topic. "
            "Ending the relationship, deleting you, or 'don't talk to me' is vent_anger, never close_topic."
        ),
        "criteria": {
            "confirm_you_care": (
                "They are testing whether you remember, pay attention, or still care. "
                "Signals: 'did you forget again', 'then say it', 'you better', sarcastic 'busy person', "
                "asking you to prove you know a past conversation. "
                "If they mainly want a new deliverable or a yes on a time, do not use this."
            ),
            "vent_anger": (
                "They are angry or hurt and mainly want the feeling acknowledged. "
                "They are blaming or raising the temperature; a specific plan is not the main point yet."
            ),
            "request_action": (
                "They want a concrete action, time, deliverable, or commitment from you now, "
                "and this is a real ask, not a loyalty test."
            ),
            "state_update": (
                "They are mainly reporting their current physical, emotional, or energy state, "
                "such as pain, discomfort, exhaustion, or feeling unwell."
            ),
            "seek_care": (
                "They are describing physical discomfort, pain, illness, exhaustion, sadness, or overwhelm "
                "and the useful response is to notice it, ask how serious it is, and show care. "
                "Do not reduce a clear health or distress signal to casual chat."
            ),
            "emotional_vent": "They are mainly letting out sadness, pressure,委屈, or frustration rather than asking for a concrete fix.",
            "seek_comfort": "They want reassurance, company, or emotional support more than facts or logistics.",
            "seek_advice": "They want your suggestion about what to do, without necessarily asking you to do it for them.",
            "seek_help": "They want you to provide concrete help, accompany them, or take something off their hands.",
            "seek_explanation": (
                "They want a factual explanation of why something happened. "
                "They asked why or what is going on, not mainly for an apology or a new plan."
            ),
            "affection": "They are expressing closeness, affection, or appreciation without an active problem.",
            "express_discontent": "They are voicing dissatisfaction or a complaint, but it is not yet a full conflict conversation.",
            "ask_information": "They are asking for information or clarification.",
            "make_plan": "They are arranging a time, place, task, or next step.",
            "rest_or_close": "They are preparing to rest, end the conversation, or leave the topic for now.",
            "unclear": "The available context is insufficient to distinguish the intent reliably; do not invent a motive.",
            "casual_chat": (
                "Light talk, banter, sharing, teasing with a laugh, or friendly logistics "
                "with no emotional test and no conflict. A friend suggesting a meal time can be this "
                "if the thread is warm."
            ),
            "close_topic": (
                "Peaceful wrap-up only: they accepted an apology, confirmed a happy plan, said thanks, "
                "or clearly signaled they need nothing more. "
                "Not a breakup, not 'don't contact me', not sarcastic 'I'm used to it'."
            ),
        },
    },
    "reply_scene": {
        "type": "choice",
        "instructions": (
            "Classify the current reply scene, not the whole relationship. Choose the most useful mode for drafting the next message. "
            "Use comfort when the other person is hurt, sad, disappointed, or needs to feel understood; "
            "this also includes physical discomfort, pain, illness, or unusual exhaustion when they need concern and company; "
            "use repair when there is a known mistake that needs apology and a concrete change; "
            "use intimacy for warm daily sharing or affectionate conversation; playful for harmless teasing; "
            "action for a concrete question, plan, or request; close when the topic is genuinely finished."
        ),
        "criteria": {
            "comfort": "They are upset, sad, hurt, lonely, disappointed, or mainly need emotional acknowledgment.",
            "repair": "A specific mistake or conflict is active; apology and a credible repair step matter.",
            "intimacy": "Warm everyday conversation, sharing, affection, or a normal romantic connection with no active conflict.",
            "playful": "Light teasing, flirting, or joking is appropriate and there is no unresolved hurt.",
            "action": "They need a concrete answer, arrangement, explanation, time, or follow-through.",
            "close": "They accepted the response or the topic is peacefully finished; do not reopen it with a long message.",
            "daily_chat": "Ordinary sharing or logistics with no meaningful distress or unresolved conflict.",
            "physical_discomfort": "The current scene is about pain, illness, discomfort, or a bodily symptom.",
            "fatigue": "The current scene is mainly tiredness, low energy, or needing rest.",
            "low_mood": "The current scene is sadness, low mood, loneliness, or emotional heaviness.",
            "stress_anxiety": "The current scene is pressure, anxiety, worry, or being mentally overloaded.",
            "hurt": "The current scene is feeling hurt,委屈, disappointed, or not cared for.",
            "anger": "The current scene is active anger or strong dissatisfaction.",
            "complaint": "The current scene is a complaint or吐槽 without a full rupture.",
            "conflict": "The current scene needs direct conflict repair or a serious conversation.",
            "flirt": "The current scene is intentional flirting or ambiguous romantic interaction.",
            "teasing": "The current scene is harmless teasing or playful banter.",
            "work": "The current scene is mainly a work or task matter.",
            "serious": "The current scene needs a serious, careful discussion rather than casual banter.",
        },
    },
    "danger_level": {
        "type": "score",
        "instructions": (
            "How much should the next reply take the other person's current state seriously, on a 0-9 scale? "
            "This is not only fight risk: physical discomfort, illness, emotional distress, and relationship conflict all count. "
            "Judge the latest relevant state from the whole conversation, not one isolated word. "
            "If they genuinely accepted an apology or confirmed a happy plan, score the cooled-down present, "
            "not an earlier complaint. "
            "If an ultimatum (break up, report to the boss, stop covering for you) is still in force "
            "and has not been withdrawn, stay in that high bin even if the latest line names a specific task."
        ),
        "criteria": [
            "Ordinary sharing, normal logistics, or joking; no meaningful discomfort, hurt, complaint, or pressure.",
            "A little tired, low-energy, or mildly awkward, but no clear complaint and no special care is needed yet.",
            "A small complaint or ordinary discomfort that is easy to handle; they are still relaxed and it does not affect plans.",
            "Clear physical discomfort or pain (for example 腰痛、头痛、肚子痛、不舒服、难受), illness, or emotional hurt; "
            "they need a caring check-in, but there is no sign of acute danger or a major fight.",
            "The discomfort or distress is repeated,明显影响活动, or they say it is quite painful/难受; ask what they need and offer concrete help.",
            "Noticeable interpersonal unhappiness, disappointment, coldness, or a meaningful complaint; a dismissive reply may escalate it.",
            "Openly upset, angry, or blaming you; they need a serious response rather than a joke or a one-word dismissal.",
            "Trust is badly strained: a last-chance warning, a serious unresolved conflict, or a problem that will not go away without action.",
            "An ultimatum is active even if they also give a practical next step: break up, report you, block you, or stop working together if this continues.",
            "Active rupture or acute danger: they said it is over, told you not to reply, are exploding, or describe an emergency needing immediate real-world help.",
        ],
    },
    "emotion_score": {
        "type": "score",
        "instructions": (
            "Score the other person's emotional distress or emotional load from 0 to 9. "
            "Use recent repeated low mood,委屈, anxiety, anger, or feeling ignored; do not infer a diagnosis."
        ),
        "criteria": [
            "No meaningful emotional load.", "Slightly tired or awkward.", "Mildly low or bothered.",
            "Clearly low,委屈, worried, or needing emotional care.", "Repeated or noticeably affecting the conversation.",
            "Strong distress or disappointment needing a serious response.", "Open anger, panic, or heavy emotional pressure.",
            "Severe unresolved emotional strain.", "Relationship-threatening emotional escalation.", "Acute crisis language or immediate danger.",
        ],
    },
    "physical_score": {
        "type": "score",
        "instructions": (
            "Score physical discomfort or illness from 0 to 9. "
            "Explicit pain,腰疼,头疼,不舒服,恶心,头晕,发烧, or lack of strength cannot be treated as ordinary small talk."
        ),
        "criteria": [
            "No physical symptom.", "Only vague low energy.", "Mild discomfort with no effect on plans.",
            "Clear pain or discomfort needing a caring check-in.", "Repeated symptoms or clearly affecting activity.",
            "Very painful, unable to function normally, or needs concrete help.", "Serious symptom needing prompt attention.",
            "Severe or worsening condition.", "Possible emergency or urgent medical concern.", "Immediate emergency; encourage real-world help.",
        ],
    },
    "urgency_score": {
        "type": "score",
        "instructions": (
            "Score how urgently the next reply should respond to the current state from 0 to 9. "
            "Resting, going home, lying down, taking leave, seeing a doctor, or being unable to continue raises urgency. "
            "Do not invent urgency when the conversation gives no such evidence."
        ),
        "criteria": [
            "No time pressure.", "Can reply normally.", "A kind check-in is enough.",
            "Should respond with care before changing topic.", "Needs a concrete check or small offer of help.",
            "Needs prompt practical support or rest.", "Needs prompt real-world attention.",
            "Should not be left to casual chat.", "Likely urgent; encourage appropriate help.", "Emergency-level urgency.",
        ],
    },
    "relationship_sensitivity_score": {
        "type": "score",
        "instructions": (
            "Score how sensitive the relationship or interpersonal meaning is from 0 to 9. "
            "Use unresolved hurt, being ignored, trust tests, anger, conflict, or an ultimatum. "
            "Physical discomfort alone raises physical/emotion scores, not relationship sensitivity."
        ),
        "criteria": [
            "No interpersonal sensitivity.", "Normal friendly exchange.", "Small awkwardness or reminder.",
            "A meaningful but repairable complaint.", "Noticeable disappointment or testing.",
            "Active unhappiness needing careful wording.", "Open anger or blame.",
            "Trust is badly strained.", "Active ultimatum.", "Rupture or explicit end of relationship.",
        ],
    },
    "should_reply_now": {
        "type": "noul",
        "instructions": (
            "Should your next message contain substantive content? "
            "Substantive means: admitting a specific known fault, giving a concrete time/plan/deliverable, "
            "explaining facts you actually know, or reciting the recalled content they asked you to say. "
            "This is NOT 'should you send any message'. Timing is irrelevant. "
            "Answer FALSE if the thing they want you to recite or prove is not present in this snippet "
            "(you would be guessing). 'Then say it' / 'you better' while you are stalling is FALSE. "
            "Answer FALSE if they already accepted and closed the topic. "
            "Answer true only if the needed fact, plan, or named fault is already in this snippet."
        ),
        "criteria": {
            "true": (
                "The needed fact, named fault, or named time/place is already in this snippet, "
                "and they are waiting for that substance now."
            ),
            "false": (
                "Do not put substance in the next message: the recalled content is not in this snippet, "
                "they are testing whether you remember, a holding line is enough, "
                "saying less is safer, or they already closed the topic."
            ),
        },
    },
    "best_action": {
        "type": "choice",
        "instructions": (
            "What type of next action is best? Do not decide whether to send a message immediately. "
            "Ignore timing. Choose only the action type. "
            "If they asked you to recall a specific past message or event and you have not shown that you actually remember it, "
            "choose check_history — do not apologize or invent a plan instead."
        ),
        "criteria": {
            "check_history": (
                "Look up prior chat or facts before taking a position. "
                "Use when they ask you to repeat, recall, or prove you remember something specific."
            ),
            "apologize": (
                "Lead with a sincere apology for a real mistake or hurt already identified. "
                "Not for an unnamed forgotten thing when you should first find out what it was."
            ),
            "give_commitment": (
                "Give a concrete promise, deadline, or arrangement they asked for "
                "in a conflict or work-pressure setting."
            ),
            "explain": (
                "Explain what happened or why, without leading with apology or a new plan."
            ),
            "acknowledge": (
                "Show you heard them and care, without new facts, an apology, or a plan. "
                "Use for light chat or when they mainly need to feel seen."
            ),
            "say_less": (
                "Keep it short or add nothing. Extra words would over-explain, reopen a closed topic, "
                "or pour fuel on an ultimatum that told you not to talk."
            ),
            "make_plan": (
                "Propose or confirm logistics (time, place, task) for a non-conflict request "
                "such as a meal or a meeting."
            ),
        },
    },
    "she_needs": {
        "type": "choice",
        "instructions": (
            "What does the other person need from you right now? Judge the LATEST message first. "
            "If they genuinely accepted (thanks / got it / 没事了 / 那就这样 / 收到了 / 过去了), "
            "you MUST choose nothing, even if earlier they wanted action or an apology. "
            "Sarcastic 'I'm used to it', 'whatever', 'I don't want to hear it', 'don't bother coming' "
            "is NOT genuine satisfaction — do not choose nothing. "
            "If they asked you to recap a named time/place/date, choose action. "
            "If they are testing whether you remember or still care, and the content is unnamed, choose care."
        ),
        "criteria": {
            "apology": (
                "They need a sincere apology for hurt or a mistake, and they have not accepted one yet."
            ),
            "action": (
                "They need a concrete action, time, commitment, recap of a named fact, or follow-through, "
                "and they have not yet accepted one."
            ),
            "explanation": (
                "They need a clear explanation of what happened or why, and have not received it."
            ),
            "care": (
                "They need proof you remember, listen, or care — a loyalty or attention test — "
                "not yet a plan or an apology. Sarcastic 'I am used to it' belongs here, not nothing."
            ),
            "nothing": (
                "They need nothing further. Genuine acceptance, a peaceful closed topic, "
                "warm casual chat with no ask, or a rupture where they told you not to reply. "
                "Not sarcasm pretending to be fine."
            ),
        },
    },
    "tension_resolved": {
        "type": "noul",
        "instructions": (
            "Has interpersonal tension already been resolved? "
            "Answer true only if there was never tension, or the other person has clearly accepted, "
            "cooled down, joked again, or said it is fine. "
            "A sarcastic 'you better', an unanswered test, leftover blame, or an open ultimatum means false."
        ),
        "criteria": {
            "true": (
                "No remaining tension: they accepted, joked again, said it's fine, "
                "confirmed a happy plan, or the chat was never tense."
            ),
            "false": (
                "Tension is still present: they are waiting, testing, angry, sarcastic, "
                "issuing an ultimatum, or the issue is open."
            ),
        },
    },
}


# 次要意图沿用同一套枚举，保留 primary_intent（true_intent）兼容原版 Jev 的字段名。
JUDGE_QUESTIONS["secondary_intent"] = {
    "type": "choice",
    "instructions": (
        "Choose one secondary intent only when it is clearly supported by the recent conversation; "
        "otherwise choose unclear. Do not invent a hidden motive just to fill the field."
    ),
    "criteria": dict(JUDGE_QUESTIONS["true_intent"]["criteria"]),
}


# choice 类答案的中文说法，界面和起草小抄共用这一份（app/overlay.py 从这里导）。
CHOICE_LABELS: dict = {
    "true_intent": {
        "confirm_you_care": "希望确认你在意", "vent_anger": "表达不满或受伤",
        "request_action": "希望你采取行动", "state_update": "告知当前状态",
        "seek_care": "希望被关心或照顾", "emotional_vent": "倾诉情绪",
        "seek_comfort": "寻求安慰", "seek_advice": "寻求建议", "seek_help": "寻求帮助",
        "seek_explanation": "希望了解原因", "affection": "表达亲近",
        "express_discontent": "表达不满", "ask_information": "询问信息",
        "make_plan": "安排计划", "rest_or_close": "准备休息或结束聊天",
        "unclear": "意图不明确", "casual_chat": "轻松交流", "close_topic": "平和结束话题",
    },
    "secondary_intent": {
        "confirm_you_care": "希望确认你在意", "vent_anger": "表达不满或受伤",
        "request_action": "希望你采取行动", "state_update": "告知当前状态",
        "seek_care": "希望被关心或照顾", "emotional_vent": "倾诉情绪",
        "seek_comfort": "寻求安慰", "seek_advice": "寻求建议", "seek_help": "寻求帮助",
        "seek_explanation": "希望了解原因", "affection": "表达亲近",
        "express_discontent": "表达不满", "ask_information": "询问信息",
        "make_plan": "安排计划", "rest_or_close": "准备休息或结束聊天",
        "unclear": "意图不明确", "casual_chat": "轻松交流", "close_topic": "平和结束话题",
    },
    "reply_scene": {
        "comfort": "安慰与接住情绪", "repair": "道歉与修复", "intimacy": "日常亲密交流",
        "playful": "轻松调侃或暧昧", "action": "具体问题或行动", "close": "收住话题",
        "daily_chat": "普通日常", "physical_discomfort": "身体不适",
        "fatigue": "疲惫劳累", "low_mood": "情绪低落", "stress_anxiety": "压力焦虑",
        "hurt": "委屈受伤", "anger": "生气不满", "complaint": "抱怨吐槽",
        "conflict": "矛盾沟通", "flirt": "暧昧互动", "teasing": "撒娇或打趣",
        "work": "工作事务", "serious": "严肃讨论",
    },
    "best_action": {
        "check_history": "先核对聊天记录", "apologize": "为已知问题道歉",
        "give_commitment": "给出具体承诺", "explain": "说明事实与原因",
        "acknowledge": "回应并表达理解", "say_less": "简短回应或留白",
        "make_plan": "商量具体安排",
    },
    "she_needs": {
        "apology": "真诚道歉", "action": "具体行动或安排", "explanation": "清楚的解释",
        "care": "关注与在意", "nothing": "可能无需补充回应",
    },
}

_GUIDE_FIELDS = (("true_intent", "主要意图"), ("secondary_intent", "补充意图"),
                 ("reply_scene", "当前场景"),
                 ("she_needs", "对方需要"),
                 ("best_action", "建议动作"))


def guidance_text(answers: dict) -> str:
    """Jev 判断 → 喂给起草的中文小抄。只写有答案的那几项；没答案返回空串。"""
    lines = []
    for name, title in _GUIDE_FIELDS:
        choice = ((answers or {}).get(name) or {}).get("choice")
        label = CHOICE_LABELS[name].get(choice)
        if label:
            lines.append(f"- {title}：{label}（{choice}）")
    tail = []
    score = ((answers or {}).get("danger_level") or {}).get("score")
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        tail.append(f"紧张度：{score:.0f}/9")
    noul = ((answers or {}).get("literal_question") or {}).get("noul")
    if isinstance(noul, (int, float)) and not isinstance(noul, bool):
        tail.append("字面意思：" + ("是" if noul >= 0.5 else "否（有潜台词）"))
    if tail:
        lines.append("- " + "；".join(tail))
    dimension_titles = (
        ("emotion_score", "情绪"), ("physical_score", "身体"),
        ("urgency_score", "紧迫"), ("relationship_sensitivity_score", "关系敏感度"),
    )
    dimensions = []
    for name, title in dimension_titles:
        value = ((answers or {}).get(name) or {}).get("score")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            dimensions.append(f"{title}{value:.0f}/9")
    if dimensions:
        lines.append("- 状态维度：" + "，".join(dimensions))
    state = (answers or {}).get("recent_state")
    if isinstance(state, dict) and state.get("physical_discomfort"):
        symptoms = "、".join(state.get("symptoms") or []) or "身体不适"
        trend = state.get("trend") or "近期出现"
        lines.append(f"- 近期状态：{symptoms}（{trend}，只引用记录中明确出现的事实）")
    if not lines:
        return ""
    return "判断参考（Jev 给的，起草要顺着它写，但口吻仍按我的）：\n" + "\n".join(lines)


def _bump_score(out: dict, name: str, floor: float) -> None:
    value = dict(out.get(name) or {})
    try:
        current = float(value.get("score", 0))
    except (TypeError, ValueError):
        current = 0.0
    value["score"] = max(float(floor), min(9.0, current))
    out[name] = value


def calibrate_answers(answers: dict, messages: list) -> dict:
    """给模型判断加一层保守的中文场景校准，避免身体不适/低落被打成 0~1。

    这是下限校准，不会把模型给出的更高分压低；只看对方（her）最近的消息，
    所以用户自己说「我腰痛」不会被误算成对方状态。它同时修正明显健康/情绪信号
    被误选成「轻松交流」的情况，但不会覆盖具体行动或解释请求。
    """
    out = dict(answers or {})
    recent = []
    for item in list(messages or [])[-12:]:
        if isinstance(item, dict):
            who, text = item.get("from"), item.get("text")
        else:
            who, text = item[0], item[1]
        if who == "her" and str(text or "").strip():
            recent.append(str(text).strip())
    if not recent:
        return out

    text = "\n".join(recent)
    severe_health = re.search(
        r"疼得厉害|痛得厉害|很疼|很痛|剧痛|不能动|动不了|走不了|发烧|高烧|呕吐|胸闷|呼吸困难|出血|受伤|去医院|急诊",
        text, re.I)
    health = re.search(
        r"腰痛|腰疼|头痛|头疼|肚子痛|肚子疼|胃痛|牙疼|牙痛|腿疼|肩颈|不舒服|不太舒服|难受|不适|疼|痛|酸痛|发烧|恶心|头晕|眩晕|累|疲惫|没精神|状态不好|状态不佳|不在状态|感觉不行|撑不住|精神不好",
        text, re.I)
    emotional = re.search(
        r"难过|委屈|想哭|崩溃|孤单|孤独|失望|心累|压力大|烦死|害怕|焦虑|睡不着|不想活|好累|累死|最近不好|最近不太好",
        text, re.I)
    rupture = re.search(r"分手|分开|别联系|不想再见|拉黑|删了你|滚开|别烦我", text, re.I)
    conflict = re.search(r"生气|气死|你怎么|不在乎|不理我|忘了|失望|敷衍|冷淡|不想理", text, re.I)
    rest_change = re.search(r"回去睡觉|去睡觉|躺着|休息|请假|回家|去医院|看医生|不能继续|先不聊", text, re.I)

    floor = 0
    if rupture:
        floor = 8
    elif severe_health:
        floor = 5
    elif health or emotional:
        floor = 3
    elif conflict:
        floor = 5
    if health or severe_health:
        signal_count = len(re.findall(
            r"腰痛|腰疼|头痛|头疼|肚子痛|肚子疼|不舒服|难受|不适|疼|痛|发烧|恶心|头晕|状态不好|不在状态|感觉不行|难过|委屈|失望|心累|压力大|好累|撑不住",
            text, re.I))
        physical_floor = 5 if severe_health else (4 if signal_count >= 2 else 3)
        floor = max(floor, physical_floor)
        _bump_score(out, "physical_score", physical_floor)
        if emotional or health:
            _bump_score(out, "emotion_score", 4 if emotional and signal_count >= 2 else 3)
        if rest_change:
            _bump_score(out, "urgency_score", 5 if severe_health else 4)
    if conflict or rupture:
        _bump_score(out, "relationship_sensitivity_score", 8 if rupture else 5)
    if floor or any(name in out for name in (
            "emotion_score", "physical_score", "urgency_score",
            "relationship_sensitivity_score")):
        danger = dict(out.get("danger_level") or {})
        try:
            current = float(danger.get("score", 0))
        except (TypeError, ValueError):
            current = 0.0
        dimensions = [float((out.get(name) or {}).get("score", 0) or 0)
                      for name in ("emotion_score", "physical_score", "urgency_score",
                                   "relationship_sensitivity_score")]
        combined = round(0.25 * dimensions[0] + 0.35 * dimensions[1]
                         + 0.20 * dimensions[2] + 0.20 * dimensions[3])
        danger["score"] = max(float(floor), min(9.0, current), float(combined))
        out["danger_level"] = danger

    intent = dict(out.get("true_intent") or {})
    if (health or emotional) and intent.get("choice") in {"casual_chat", "close_topic"}:
        # 单纯陈述「腰痛/感觉不行/最近状态不好」优先归为状态告知；
        # 对方是否需要照顾作为 secondary_intent 补充，避免把事实报告也笼统叫成闲聊。
        intent["choice"] = "state_update" if health else "emotional_vent"
        intent["confidence"] = max(float(intent.get("confidence", 0.0) or 0.0), 0.65)
        out["true_intent"] = intent
    secondary = dict(out.get("secondary_intent") or {})
    if (health or emotional) and secondary.get("choice") in {None, "casual_chat", "close_topic", "unclear"}:
        secondary["choice"] = "seek_care"
        secondary["confidence"] = max(float(secondary.get("confidence", 0.0) or 0.0), 0.65)
        out["secondary_intent"] = secondary
    # 让判断和起草都能拿到同一份近期状态摘要；不做长期人格推断。
    out["recent_state"] = recent_state(messages)
    return out


def recent_state(messages: list, max_messages: int = 40) -> dict:
    """从最近最多 40 条对方消息提取短期状态信号，不读取或保存长期历史。"""
    rows = []
    for item in list(messages or [])[-max_messages:]:
        if isinstance(item, dict):
            who, text = item.get("from"), item.get("text")
        else:
            who, text = item[0], item[1]
        if who == "her" and str(text or "").strip():
            rows.append(str(text).strip())
    joined = "\n".join(rows)
    symptom_map = (
        ("腰部疼痛", r"腰痛|腰疼|腰酸|腰不舒服"),
        ("头部不适", r"头痛|头疼|头晕|眩晕"),
        ("身体不适", r"不舒服|难受|不适|疼|痛|酸痛|恶心|发烧|没力气"),
        ("疲惫或状态下降", r"累|疲惫|没精神|状态不好|状态不佳|不在状态|感觉不行|撑不住"),
        ("情绪低落或压力", r"难过|委屈|想哭|崩溃|孤单|失望|心累|压力大|焦虑|睡不着"),
    )
    symptoms = [label for label, pattern in symptom_map if re.search(pattern, joined, re.I)]
    signal_hits = len(re.findall(
        r"腰痛|腰疼|头痛|头疼|不舒服|难受|不适|疼|痛|发烧|恶心|头晕|状态不好|不在状态|感觉不行|难过|委屈|失望|心累|压力大|好累|撑不住",
        joined, re.I))
    repeated = signal_hits >= 2 or len(rows) >= 3 and bool(symptoms)
    return {
        "physical_discomfort": bool(re.search(
            r"腰痛|腰疼|头痛|头疼|肚子痛|肚子疼|不舒服|难受|不适|疼|痛|发烧|恶心|头晕|没力气",
            joined, re.I)),
        "symptoms": symptoms,
        "recently_repeated": repeated,
        "trend": "持续或反复" if repeated else ("近期出现" if symptoms else "未见明确状态信号"),
        "confidence": 0.85 if repeated else (0.65 if symptoms else 0.2),
        "messages_considered": len(rows),
    }


def build_state(messages: list, relationship: str, keep: int = 10,
                reply_to: str | None = None) -> dict:
    """messages: (from, text) / (from, text, name) / dict（name 可选）。from 只认 her/me。

    name = 群里的发言人；有 name 就当群聊（chat.is_group）。reply_to = 群里指定的回复对象。
    """
    cleaned = []
    for item in messages:
        if isinstance(item, dict):
            who, text, name = item.get("from"), item.get("text"), item.get("name")
        else:
            who, text = item[0], item[1]
            name = item[2] if len(item) > 2 else None
        if who not in ("her", "me"):
            raise ValueError(f"message from must be 'her' or 'me', got {who!r}")
        message = {"from": who, "text": str(text)}
        if name:
            message["name"] = str(name)
        cleaned.append(message)
    state_summary = recent_state(cleaned)
    cleaned = cleaned[-keep:]
    latest_from = cleaned[-1]["from"] if cleaned else "her"
    chat = {
        "relationship": relationship,
        "messages": cleaned,
        "recent_state": state_summary,
        "latest_from": latest_from,
        "is_group": any("name" in m for m in cleaned),
    }
    if reply_to:
        chat["reply_to"] = str(reply_to)
    return {"chat": chat}


def build_rank_question(candidates: list[str]) -> dict:
    """Build the best_reply choice question. criteria values stay in original Chinese."""
    if not 2 <= len(candidates) <= 3:
        raise ValueError("build_rank_question expects 2 or 3 candidate replies")
    keys = ("reply_a", "reply_b", "reply_c")[:len(candidates)]
    return {
        "best_reply": {
            "type": "choice",
            "instructions": (
            "Which candidate reply is the most appropriate next message, "
            "given the conversation and the other person's true need? "
            "Prefer a reply that matches the best action type. "
            "Evaluate context_fit, relationship_fit, emotion_fit, specificity, naturalness, "
            "and penalize unsupported assumptions, AI/counselor tone, pressure, and over-promising. "
            "A candidate that guesses a cause not present in the conversation must rank below one that asks a careful check-in. "
            "If the facts are not yet confirmed, prefer the candidate that looks them up "
                "instead of faking memory or a vague apology."
            ),
            "criteria": {key: text for key, text in zip(keys, candidates)},
        }
    }
