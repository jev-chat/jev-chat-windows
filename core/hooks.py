# -*- coding: utf-8 -*-
"""engine 管线的扩展钩子：默认全部空实现，行为与原版一致。

插件（core/strategy/）在启动时把这些函数替换为自己的实现（install()）。
engine 只认钩子签名、不认插件；钩子文件缺失（旧版升级/打包裁剪）时 engine 跳过调用。

约定：
- 所有钩子都在 try/except 里被调用：钩子抛异常 = 该项增强失效，主流程继续
- enrich_state 返回 None = 用原 state
- judge_questions 返回 None = 用内置 JUDGE_QUESTIONS
- augment_guidance 收到 None 时返回 None（盲起草路径不注小抄）
- after_analyze 必须返回 result dict
"""
from __future__ import annotations


def enrich_state(state, messages, relationship, chat_key, context=10):
    """判断前增强 state（域路由、策略召回、历史概括注入）。默认原样。"""
    return state


def judge_questions():
    """替换首轮判断的题集。None = 用内置 JUDGE_QUESTIONS。"""
    return None


def augment_guidance(text, answers):
    """给起草小抄（guidance_text 产物）追加内容。None 透传 = 盲起草不加小抄。"""
    return text


def after_analyze(result, *, state=None, answers=None, judged=False, messages=None,
                  relationship="", context=10, timeout=20, chat_key=None,
                  jev_provider="openrouter", jev_model=None):
    """分析完成后的后处理（仲裁、第二跳、结果装饰）。必须返回 result。"""
    return result
