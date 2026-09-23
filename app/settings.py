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

from core.providers import CUSTOM, DRAFT_PROVIDERS, JEV_ENV, JEV_PROVIDERS, LEGACY, LLM_ENV

_ROOT = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
         else os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CONFIG = os.path.join(_ROOT, "config.json")
_DEFAULT_RELATIONSHIP = "romantic partners"
_DEFAULT_CONTEXT = 10
_DEFAULT_JEV = "openrouter"
_DEFAULT_DRAFT = "deepseek"


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

def style() -> str:
    """用户自己描述的说话风格（可选，自由文本），只喂给起草模型。默认空 = 只照着最近的消息模仿。"""
    return str(_read("style") or "")

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
    """起草请求的 Base URL；空 = 用来源表里的默认地址。设置页常驻这一行、切来源自动填默认值，
    用户改过就按改的来（预设也能覆盖地址，比如换成镜像）。"""
    return str(_read("draft_base_url") or "")

def jev_base_url() -> str:
    """判断请求的 Base URL；空 = 用来源表里的默认地址。同理可对任意来源手动覆盖。"""
    return str(_read("jev_base_url") or "")

def draft_extra() -> dict | None:
    """起草请求的高级参数（用户填的 JSON），None = 没填或不是合法 JSON 对象；浅合并进请求体。"""
    raw = draft_extra_text().strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) and data else None

def draft_extra_text() -> str:
    """高级参数的原文（设置页回显用），没填是空串。"""
    return str(_read("draft_extra") or "")

def snap_follow() -> bool:
    """悬浮窗吸附跟随微信窗口：默认开。关了就停在用户拖到的地方。"""
    return bool(_read("snap_follow", True))

def bubble_layer() -> bool:
    """微信聊天区气泡浮层（对方消息 + Jev 判断 + 三条候选画在微信上）：默认开。"""
    return bool(_read("bubble_layer", True))

def vision_draft() -> bool:
    """起草用视觉模型读屏：开了以后聊天区截图直接发给起草模型（需模型支持图片输入）。
    会话名识别和触发检测仍用本地 OCR——判断/排序也还需要文字。"""
    return bool(_read("vision_draft", False))

def filter_system_msgs() -> bool:
    """过滤群聊系统通知（进群/退群/撤回/时间戳）：默认开。
    开 = OCR 入口直接不进上下文 + 给起草模型注入忽略提示词；关 = 原样送进分析。"""
    return bool(_read("filter_system_msgs", True))

def bubble_offset():
    """浮层被用户拖离默认锚点的偏移 [dx, dy]（逻辑 px）；None = 没拖过，用默认位置。"""
    v = _read("bubble_offset")
    try:
        dx, dy = float(v[0]), float(v[1])
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    return (dx, dy)

def reply_target() -> bool:
    """群聊指定回复对象：开了才在界面上选回复给谁、才把对象喂给模型。默认关。"""
    return bool(_read("reply_target", False))

def thinking() -> bool:
    """起草时是否开思考模式：慢且贵，默认关。只有 DeepSeek / OpenRouter / Anthropic / Gemini 吃它。"""
    return bool(_read("thinking", False))

def check_update() -> bool:
    """启动时要不要去 GitHub 查一次最新版本号：默认开，只出这一次网，设置里能关。"""
    return bool(_read("check_update", True))

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
        # 广播一下，之后新开的终端/进程就能看到；已经开着的 IDE 看不到也无所谓，启动时会读注册表
        ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x1A, 0, "Environment", 2, 5000, None)
    except Exception:
        pass  # 非 Windows（本机 Mac 开发）走不到，忽略

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

has_key = has_jev_key  # 旧名字：界面上「配没配好」问的就是判断模型这把 key

def save(relationship_text: str | None = None, context_n: int | None = None, *,
         jev_provider_text: str | None = None, jev_key_text: str | None = None,
         jev_model_text: str | None = None, jev_base_url_text: str | None = None,
         draft_provider_text: str | None = None,
         llm_key_text: str | None = None, draft_model_text: str | None = None,
         draft_base_url_text: str | None = None, draft_extra_text: str | None = None,
         reply_target_on: bool | None = None,
         style_text: str | None = None, thinking_on: bool | None = None,
         check_update_on: bool | None = None, debug_view_on: bool | None = None,
         snap_follow_on: bool | None = None, bubble_layer_on: bool | None = None,
         filter_system_msgs_on: bool | None = None,
         vision_draft_on: bool | None = None,
         bubble_offset_pair: list | tuple | None = None) -> None:
    """每个参数为空/None = 保留当前值。两把 key 写进程环境 + HKCU\\Environment，不写任何文件。"""
    jev = jev_provider_text if jev_provider_text in JEV_PROVIDERS else jev_provider()
    draft = draft_provider_text if draft_provider_text in DRAFT_PROVIDERS else draft_provider()
    # 没重填就把老变量里的值抄进新名字，迁移一次性做完（_get_key 已经退回读过老的了）
    for env, typed in ((JEV_ENV, jev_key_text), (LLM_ENV, llm_key_text)):
        value = typed or ("" if _read_env(env) else _get_key(env))
        if value:
            _set_key(env, value)
    n = context() if context_n is None else max(3, min(30, int(context_n)))
    # 空串 = 清掉，None = 原样留着（读原始字段，别读补过默认值的那个）
    keep = lambda new, name: str(_read(name) or "") if new is None else str(new).strip()
    flag = lambda new, now: now() if new is None else bool(new)
    # 整个 dict 必须在 open(..., "w") **之前**拼好：open 一上来就把文件截断，
    # 之后再 _read() 读到的是空文件，None 那几项就不是「保留」而是被清空了。
    data = {
        # 关系为空 = 只改别的开关（调试视图那种单项保存），别把它写没了
        "relationship": relationship_text or relationship(), "context": n,
        "style": keep(style_text, "style"),
        "jev_provider": jev, "jev_model": keep(jev_model_text, "jev_model"),
        "draft_provider": draft, "draft_model": keep(draft_model_text, "draft_model"),
        "draft_base_url": keep(draft_base_url_text, "draft_base_url"),
        "jev_base_url": keep(jev_base_url_text, "jev_base_url"),
        "draft_extra": keep(draft_extra_text, "draft_extra"),
        "reply_target": flag(reply_target_on, reply_target),
        "thinking": flag(thinking_on, thinking),
        "check_update": flag(check_update_on, check_update),
        "debug_view": flag(debug_view_on, debug_view),
        "snap_follow": flag(snap_follow_on, snap_follow),
        "bubble_layer": flag(bubble_layer_on, bubble_layer),
        "filter_system_msgs": flag(filter_system_msgs_on, filter_system_msgs),
        "vision_draft": flag(vision_draft_on, vision_draft),
        # 浮层拖拽偏移：None = 原样保留；[dx, dy] = 存下（拖回默认位就存 [0, 0]，效果一样）
        "bubble_offset": list(bubble_offset_pair) if bubble_offset_pair is not None
        else (_read("bubble_offset") or [0, 0]),
    }
    # 原子写：先写临时文件再 os.replace，中途崩了也不会留下半截 config.json
    # （截断的 JSON 读出来是默认值，看起来就是「设置没保存」）
    tmp = _CONFIG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, _CONFIG)
