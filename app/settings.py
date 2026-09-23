# -*- coding: utf-8 -*-
"""设置持久化。key 硬约束（docs/KICKOFF.md #6）：只进环境变量，绝不落文件；其余设置落 config.json。

key 的持久化走 Windows 用户环境变量（注册表 HKCU\\Environment，跟 setx 写的是同一个地方）。
全程只有两把：判断 JEV_API_KEY、起草 LLM_API_KEY，跟选哪家来源无关。
读的时候先看进程环境，没有就直接读注册表——IDE 启动时把环境快照拿走了，之后再 Run 继承的还是旧环境，
只靠 os.environ 会「保存了下次打开还是没有」。"""
from __future__ import annotations

import ctypes
import json
import os
import sys  # 只为下面这一处：打包后 __file__ 指向临时解包目录，config.json 得放在 exe 旁边才存得住

from core.providers import (CUSTOM, DRAFT_PROVIDERS, ENV_VARS, JEV_ENV, JEV_PROVIDERS,
                            LEGACY, LLM_ENV)

_ROOT = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
         else os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CONFIG = os.path.join(_ROOT, "config.json")
_DEFAULT_RELATIONSHIP = "romantic partners"
_DEFAULT_CONTEXT = 10
_DEFAULT_JEV = "openrouter"
_DEFAULT_DRAFT = "deepseek"
_DEFAULT_CHAT_APP = "wechat"
_DEFAULT_JUDGE_MODE = "follow"
_DEFAULT_REPLY_PROFILE = "auto"
_DEFAULT_HISTORY_ENABLED = True
_DEFAULT_HISTORY_LIMIT = 40
_DEFAULT_HISTORY_DAYS = 7
_DEFAULT_HISTORY_MAX_MB = 50
_RULES_FILE = os.path.join(_ROOT, "rules", "reply_strategy.md")
_RULE_FILES = (
    "relationships/girlfriend.md", "relationships/pursuing.md", "relationships/ambiguous.md",
    "relationships/friend.md", "relationships/colleague.md", "relationships/family.md",
    "scenes/comfort.md", "scenes/conflict.md", "scenes/daily_chat.md", "scenes/flirt.md",
)
DEFAULT_REPLY_GUIDE = """# 默认关系回复规则

## 先识别场景
- 先判断当前更像安慰、道歉修复、日常亲密、轻松调侃、具体行动还是收住话题，再决定怎么回。
- 对方难过、受伤、失望或生气时，先回应感受，不急着讲道理、解释或开玩笑。
- 有明确过错时，承认自己造成的影响，再给一个具体且做得到的修复动作，不空头承诺。
- 对方只是分享、撒娇或正常聊天时，像本人自然接话，不要每句话都上升成安慰或长篇分析。
- 对方已经接受或话题结束时，简短收住，不为了凑回复重新翻旧账。

## 按关系把握分寸
- 女友 / 恋人：可以亲近、表达在意和陪伴；不要用肉麻称呼代替真正回应。
- 追求对象：表达关心但尊重分寸，不默认已经是恋人，不用占有欲或过度承诺施压。
- 暧昧对象：可以温暖、带一点自然暧昧，但给对方留空间，不把暧昧说成确定关系。
- 朋友：亲切自然，少用恋人式安慰、告白或占有式表达。
- 同事：清楚、礼貌、以事实和安排为主，不越界暧昧或过度热情。

## 表达方式
- 像真实聊天，不像客服、心理咨询师、作文或鸡汤；少总结，少套话，优先模仿我平时的口吻。
- 不说“你想太多”“别生气了”来压住情绪；不确定的事实先问清楚，不编造记忆。
- 每条候选都必须是可以直接发给对方的话，不要评论模型、软件、回复质量或分析过程。"""


def default_reply_guide() -> str:
    """读取随程序发布的完整 Markdown 规则；文件缺失时使用内置兜底文本。"""
    roots = (_ROOT, os.path.join(_ROOT, "_internal"))
    parts = []
    for relative in _RULE_FILES:
        for root in roots:
            try:
                with open(os.path.join(root, "rules", relative), encoding="utf-8") as f:
                    value = f.read().strip()
                if value:
                    parts.append(value)
                    break
            except OSError:
                continue
    if parts:
        return "\n\n---\n\n".join(parts)
    try:
        with open(_RULES_FILE, encoding="utf-8") as f:
            value = f.read().strip()
        if value:
            return value
    except OSError:
        pass
    return DEFAULT_REPLY_GUIDE


def _read(name: str, default=None):
    """读 config.json 里的一个字段；每次都重新读文件，改设置不用重启进程。"""
    try:
        with open(_CONFIG, encoding="utf-8") as f:
            value = json.load(f).get(name)
    except (OSError, ValueError):
        return default
    return default if value is None else value

def relationship() -> str:
    return str(_read("relationship") or _DEFAULT_RELATIONSHIP)

def context() -> int:
    """参考上下文条数：起草和判断各看最近多少条消息。3~30，缺失/脏数据一律退默认值。"""
    try:
        n = int(_read("context", _DEFAULT_CONTEXT))
    except (TypeError, ValueError):
        return _DEFAULT_CONTEXT
    return max(3, min(30, n))


def history_enabled() -> bool:
    """是否在重启后恢复本机会话的文字历史。"""
    return bool(_read("history_enabled", _DEFAULT_HISTORY_ENABLED))


def history_limit() -> int:
    """每个会话最多恢复多少条历史文字；模型实际仍受「参考上下文」限制。"""
    try:
        n = int(_read("history_limit", _DEFAULT_HISTORY_LIMIT))
    except (TypeError, ValueError):
        return _DEFAULT_HISTORY_LIMIT
    return max(10, min(200, n))


def history_retention_days() -> int:
    try:
        n = int(_read("history_retention_days", _DEFAULT_HISTORY_DAYS))
    except (TypeError, ValueError):
        return _DEFAULT_HISTORY_DAYS
    return max(1, min(3650, n))


def history_max_mb() -> int:
    try:
        n = int(_read("history_max_mb", _DEFAULT_HISTORY_MAX_MB))
    except (TypeError, ValueError):
        return _DEFAULT_HISTORY_MAX_MB
    return max(1, min(2048, n))

def style() -> str:
    """用户自己描述的说话风格（可选，自由文本），只喂给起草模型。默认空 = 只照着最近的消息模仿。"""
    return str(_read("style") or "")


def reply_profile() -> str:
    """起草策略：auto / natural / girlfriend / repair / playful。"""
    value = str(_read("reply_profile") or "").lower()
    return value if value in {"auto", "natural", "girlfriend", "repair", "playful"} else _DEFAULT_REPLY_PROFILE


def reply_guide() -> str:
    """用户自定义的恋爱/说话规则，只作为起草偏好，不改变安全和输出格式。"""
    value = _read("reply_guide", None)
    return default_reply_guide() if value is None or not str(value).strip() else str(value)


def custom_reply_guide() -> str:
    """只返回用户真正改过的规则；内置规则由起草层按关系/场景选择，避免重复烧输入 token。"""
    value = str(_read("reply_guide", "") or "").strip()
    if not value or value == default_reply_guide().strip():
        return ""
    return value

def chat_app() -> str:
    """当前采集的聊天软件：wechat 或 qq。"""
    value = str(_read("chat_app") or "").lower()
    return value if value in {"wechat", "qq"} else _DEFAULT_CHAT_APP

def chat_app_name() -> str:
    return "QQ" if chat_app() == "qq" else "微信"

def judge_mode() -> str:
    """判断/排序走原版 Jev，或跟随起草模型；旧版 deepseek 自动并入跟随。"""
    value = str(_read("judge_mode") or "").lower()
    if value == "deepseek":
        return "follow"
    return value if value in {"jev", "follow"} else _DEFAULT_JUDGE_MODE

def judge_model() -> str:
    """跟随判断使用起草配置里的模型；原版 Jev 用独立模型。"""
    if judge_mode() == "follow":
        return draft_model()
    if judge_mode() != "deepseek":
        return jev_model()
    return draft_model() if draft_provider() == "deepseek" else "deepseek-flash"

def judge_base_url() -> str:
    if judge_mode() == "follow":
        return draft_base_url() or DRAFT_PROVIDERS[draft_provider()].base
    return ""

def jev_provider() -> str:
    """判断模型走哪家：openrouter（默认）或 typesafe 直连。"""
    v = _read("jev_provider")
    return v if v in JEV_PROVIDERS else _DEFAULT_JEV

def jev_model() -> str:
    """判断模型 id；空 = 用该来源的默认模型。"""
    return str(_read("jev_model") or "") or JEV_PROVIDERS[jev_provider()].default

def draft_provider() -> str:
    """起草走哪家（见 core/providers.DRAFT_PROVIDERS）。老配置里的 openrouter/deepseek 照样认。"""
    v = _read("draft_provider")
    return v if v in DRAFT_PROVIDERS else _DEFAULT_DRAFT

def draft_provider_name() -> str:
    return DRAFT_PROVIDERS[draft_provider()].name

def draft_model() -> str:
    """起草模型 id；空 = 用该来源的默认模型（有的来源没有默认，那就得自己选）。"""
    return str(_read("draft_model") or "") or DRAFT_PROVIDERS[draft_provider()].default

def draft_base_url() -> str:
    """自定义来源的 Base URL；其余来源用表里的，这里返回空。"""
    return str(_read("draft_base_url") or "") if draft_provider() in CUSTOM else ""

def reply_target() -> bool:
    """群聊指定回复对象：开了才在界面上选回复给谁、才把对象喂给模型。默认关。"""
    return bool(_read("reply_target", False))

def thinking() -> bool:
    """起草时是否开思考模式：慢且贵，默认关。只有 DeepSeek / OpenRouter / Anthropic / Gemini 吃它。"""
    return bool(_read("thinking", False))

def check_update() -> bool:
    """修改版不自动检查原版更新，避免把原版更新误当成修改版更新。"""
    return False

def debug_view() -> bool:
    """调试视图：另开一个窗口实时画识别框。默认关，开了子进程才往队列里送帧。"""
    return bool(_read("debug_view", False))

def _read_env(env_name: str) -> str:
    """进程环境优先；没有就读注册表并带进进程环境，之后 core/ 里按 os.environ 读就有了。"""
    v = os.environ.get(env_name, "").strip()
    if not v:
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                v = str(winreg.QueryValueEx(k, env_name)[0]).strip()
        except Exception:  # 非 Windows / 没这个值
            v = ""
        if v:
            os.environ[env_name] = v
    return v

def _get_key(env_name: str) -> str:
    """两把 key 之一。新名字空着就退回老版本按来源存的变量（下次保存会抄进新名字）。"""
    return _read_env(env_name) or _read_env(LEGACY[env_name])

def _set_key(env_name: str, value: str) -> None:
    """只写进程环境 + HKCU\\Environment，不写任何文件。"""
    os.environ[env_name] = value
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, env_name, 0, winreg.REG_SZ, value)
        # 异步广播一下，避免保存设置时被某个无响应窗口卡住 Qt 主线程。
        ctypes.windll.user32.SendNotifyMessageW(0xFFFF, 0x1A, 0, "Environment")
    except Exception:
        pass  # 非 Windows（本机 Mac 开发）走不到，忽略


def _clear_key(env_name: str) -> None:
    """清掉一个明确的本应用/旧版兼容环境变量，不触碰其它注册表内容。"""
    os.environ.pop(env_name, None)
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                            winreg.KEY_SET_VALUE) as k:
            try:
                winreg.DeleteValue(k, env_name)
            except FileNotFoundError:
                pass
    except Exception:
        pass


def reset() -> None:
    """恢复分享用的干净默认设置，并清空新旧版本的全部 API key 和历史。"""
    for env_name in ENV_VARS:
        _clear_key(env_name)
    data = {
        "relationship": _DEFAULT_RELATIONSHIP,
        "context": _DEFAULT_CONTEXT,
        "style": "",
        "reply_profile": _DEFAULT_REPLY_PROFILE,
        "reply_guide": default_reply_guide(),
        "jev_provider": _DEFAULT_JEV,
        "jev_model": "",
        "draft_provider": _DEFAULT_DRAFT,
        "draft_model": "",
        "draft_base_url": "",
        "chat_app": _DEFAULT_CHAT_APP,
        "judge_mode": _DEFAULT_JUDGE_MODE,
        "reply_target": False,
        "thinking": False,
        "check_update": False,
        "debug_view": False,
        "history_enabled": _DEFAULT_HISTORY_ENABLED,
        "history_limit": _DEFAULT_HISTORY_LIMIT,
        "history_retention_days": _DEFAULT_HISTORY_DAYS,
        "history_max_mb": _DEFAULT_HISTORY_MAX_MB,
    }
    with open(_CONFIG, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    try:
        from core.history import clear_all

        clear_all()
    except Exception:
        # 重置设置不能因为旧历史库损坏而失败；历史模块会在下次使用时自愈。
        pass

def jev_key() -> str:
    """判断那把 key，两家来源共用。"""
    return _get_key(JEV_ENV)

def has_jev_key() -> bool:
    return bool(jev_key())

def llm_key() -> str:
    """起草那把 key，所有语言模型来源共用。"""
    return _get_key(LLM_ENV)

def has_llm_key() -> bool:
    return bool(llm_key())

def has_judge_key() -> bool:
    return has_llm_key() if judge_mode() == "follow" else has_jev_key()

has_key = has_judge_key  # 兼容旧调用：按当前判断模式检查对应 key

def save(relationship_text: str | None = None, context_n: int | None = None, *,
         jev_provider_text: str | None = None, jev_key_text: str | None = None,
         jev_model_text: str | None = None, draft_provider_text: str | None = None,
         llm_key_text: str | None = None, draft_model_text: str | None = None,
         draft_base_url_text: str | None = None, reply_target_on: bool | None = None,
         style_text: str | None = None, thinking_on: bool | None = None,
         check_update_on: bool | None = None, debug_view_on: bool | None = None,
         chat_app_text: str | None = None, judge_mode_text: str | None = None,
         reply_profile_text: str | None = None, reply_guide_text: str | None = None,
         history_enabled_on: bool | None = None, history_limit_n: int | None = None,
         history_days_n: int | None = None, history_max_mb_n: int | None = None) -> None:
    """每个参数为空/None = 保留当前值。两把 key 写进程环境 + HKCU\\Environment，不写任何文件。"""
    jev = jev_provider_text if jev_provider_text in JEV_PROVIDERS else jev_provider()
    draft = draft_provider_text if draft_provider_text in DRAFT_PROVIDERS else draft_provider()
    app = chat_app_text if chat_app_text in {"wechat", "qq"} else chat_app()
    mode = judge_mode_text if judge_mode_text in {"jev", "follow"} else judge_mode()
    profile = (reply_profile_text if reply_profile_text in
               {"auto", "natural", "girlfriend", "repair", "playful"} else reply_profile())
    # 没重填就把老变量里的值抄进新名字，迁移一次性做完（_get_key 已经退回读过老的了）
    for env, typed in ((JEV_ENV, jev_key_text), (LLM_ENV, llm_key_text)):
        value = typed or ("" if _read_env(env) else _get_key(env))
        if value:
            _set_key(env, value)
    n = context() if context_n is None else max(3, min(30, int(context_n)))
    history_n = history_limit() if history_limit_n is None else max(10, min(200, int(history_limit_n)))
    history_days = (history_retention_days() if history_days_n is None
                    else max(1, min(3650, int(history_days_n))))
    history_mb = (history_max_mb() if history_max_mb_n is None
                  else max(1, min(2048, int(history_max_mb_n))))
    # 空串 = 清掉，None = 原样留着（读原始字段，别读补过默认值的那个）
    keep = lambda new, name: str(_read(name) or "") if new is None else str(new).strip()
    flag = lambda new, now: now() if new is None else bool(new)
    # 整个 dict 必须在 open(..., "w") **之前**拼好：open 一上来就把文件截断，
    # 之后再 _read() 读到的是空文件，None 那几项就不是「保留」而是被清空了。
    data = {
        # 关系为空 = 只改别的开关（调试视图那种单项保存），别把它写没了
        "relationship": relationship_text or relationship(), "context": n,
        "style": keep(style_text, "style"),
        "reply_profile": profile,
        "reply_guide": keep(reply_guide_text, "reply_guide"),
        "jev_provider": jev, "jev_model": keep(jev_model_text, "jev_model"),
        "draft_provider": draft, "draft_model": keep(draft_model_text, "draft_model"),
        "draft_base_url": keep(draft_base_url_text, "draft_base_url"),
        "chat_app": app, "judge_mode": mode,
        "reply_target": flag(reply_target_on, reply_target),
        "thinking": flag(thinking_on, thinking),
        "debug_view": flag(debug_view_on, debug_view),
        "history_enabled": flag(history_enabled_on, history_enabled),
        "history_limit": history_n,
        "history_retention_days": history_days,
        "history_max_mb": history_mb,
        # 修改版不再保存/启用原版自动更新开关。
        "check_update": False,
    }
    with open(_CONFIG, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
