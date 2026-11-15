"""Turn a user's recording into a validated, annotated script."""

import json

from libs import ai_client, signin_pcbeta
from libs.signin_workbench import (
    WorkbenchError, clean_chat, credentials_in_chat, page_label, pcbeta_daily, pcbeta_daily_script, validate_script,
)

CHAT_CONTEXT = (
    "你是 QD 签到助手。用用户的语言简洁回答，解释如何在浏览器中完成登录和验证、录制签到及定时。"
    "用户亲自完成站点要求的验证。不要询问密码、验证码、Cookie 或令牌。"
    "你无法打开网站、执行代码、操作浏览器或修改任务。"
)

async def chat_reply(history, client=None):
    if not isinstance(history, list) or not 1 <= len(history) <= 12:
        raise ValueError("对话消息格式无效")
    messages = [{"role": "system", "content": CHAT_CONTEXT}]
    for item in history:
        if not isinstance(item, dict) or item.get("role") not in ("assistant", "user"):
            raise ValueError("对话消息格式无效")
        text = item.get("content")
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise ValueError("对话消息内容无效")
        if item["role"] == "user" and credentials_in_chat(text):
            raise WorkbenchError("检测到凭据或验证码，消息已拦截。请在浏览器页面中输入验证信息")
        messages.append({"role": item["role"], "content": clean_chat(text)})
    if messages[-1]["role"] != "user":
        raise ValueError("对话消息格式无效")
    client = client or ai_client.AIClient()
    reply = await client.chat(messages)
    return clean_chat(reply)[:1800]


async def analyze_recording(recording, client=None):
    client = client or ai_client.AIClient()
    if not client.enabled:
        raise WorkbenchError("请先配置自建模型服务，再分析录制结果")
    if not recording.summaries:
        raise WorkbenchError("没有录到同站点签到请求，请重新录制")
    observed = [{"id": index, "tag": step["tag"]} for index, step in enumerate(recording.steps, 1)]
    summary = {
        "requests": recording.summaries,
        "actions": observed,
        "site_reported_success": recording.success,
    }
    order_instruction = (
        "自动录制的全部操作都需要保留，逐条按原顺序标注。"
        if recording.auto_started
        else "所有操作都来自用户录制，按原顺序列出，可省略与签到无关的操作，首尾操作必须保留。"
    )
    reply = await client.chat(
        [
            {
                "role": "system",
                "content": (
                    "你在协助生成 QD 签到脚本。依据已脱敏的请求摘要和浏览器操作编号写出步骤说明。"
                    + order_instruction
                    + "请求中的 after_action 指前一次操作编号。"
                    '只输出 JSON：{"summary":"简短说明", "steps":[{"id":1,"label":"操作说明"}]}。'
                    "信息不足时写‘执行录制的操作’，不要猜测目标、成功结果、凭据或网站功能。"
                ),
            },
            {"role": "user", "content": json.dumps(summary, ensure_ascii=False)},
        ],
        temperature=0.1,
    )
    if not isinstance(reply, str) or not reply.strip():
        raise WorkbenchError("模型返回空结果，可重新分析")
    ids = list(range(1, len(observed) + 1))
    labels = [f"执行录制的操作 {index}" for index in ids]
    description = reply
    try:
        plan = ai_client.parse_ai_response(reply)
    except ai_client.AIClientError:
        plan = None
    if isinstance(plan, dict):
        if isinstance(plan.get("summary"), str):
            description = plan["summary"]
        steps = plan.get("steps")
        if isinstance(steps, list) and steps and len(steps) <= len(observed) and all(
            isinstance(step, dict) and type(step.get("id")) is int and isinstance(step.get("label"), str)
            for step in steps
        ):
            selected = [step["id"] for step in steps]
            if selected == sorted(set(selected)) and selected[0] == 1 and selected[-1] == len(observed):
                if not recording.auto_started:
                    ids = selected
                supplied = {step["id"]: clean_chat(step["label"].strip())[:80] for step in steps}
                labels = [supplied.get(index) or f"执行录制的操作 {index}" for index in ids]
    script = validate_script({
        "origin": recording.origin,
        "url": recording.start_url,
        "steps": [recording.steps[index - 1] for index in ids],
        "labels": labels,
    })
    return {
        "summary": clean_chat(description)[:1600],
        "steps": len(ids),
        "requests": len(recording.summaries),
        "blocked": recording.blocked,
        "verified": recording.success,
        "site": page_label(recording.start_url),
        "script": script,
        "full_steps": ids == list(range(1, len(observed) + 1)),
    }


async def record_workflow(session, mode, client=None):
    if session.taskid is not None or session.templateid is not None:
        raise WorkbenchError("此会话用于给现有脚本绑定登录状态")
    if mode == "full":
        if not session.draft or not session.captured or session.saved:
            raise WorkbenchError("没有可恢复的录制草稿")
        steps = session.captured.steps
        session.draft["script"]["steps"] = steps
        session.draft["script"]["labels"] = [f"执行录制的操作 {index}" for index in range(1, len(steps) + 1)]
        session.draft.update(steps=len(steps), full_steps=True)
        return {"draft": session.draft}
    if mode == "cancel":
        session.recording = None
        return {"ok": True}
    if mode == "auto" and page_label(session.start_url) in (signin_pcbeta.ORIGIN, "http://i.pcbeta.com"):
        if session.recording is not None:
            raise WorkbenchError("请先结束当前录制")
        session.draft = session.captured = session.saved = session.created_task = None
        receipt = await pcbeta_daily(session.active_page(), session.context)
        script = pcbeta_daily_script()
        session.draft = {
            "summary": receipt,
            "steps": len(script["steps"]),
            "requests": 0,
            "blocked": 0,
            "verified": True,
            "site": signin_pcbeta.ORIGIN,
            "script": script,
            "full_steps": True,
        }
        return {"draft": session.draft}
    client = client or ai_client.AIClient()
    if mode in ("auto", "manual"):
        if not client.enabled:
            raise WorkbenchError("请先配置自建模型服务")
        await session.begin()
        if mode == "manual":
            return {"manual": True}
        reason = await session.auto_record()
        if reason:
            count = len(session.recording.steps)
            return {"manual": True, "reason": reason, "recorded_steps": count}
        await session.finish_recording()
    elif mode == "finish":
        if session.recording:
            await session.finish_recording()
    elif mode != "retry":
        raise ValueError("未知的录制操作")
    if not session.captured:
        raise WorkbenchError("请先录制一次签到")
    if session.saved:
        raise WorkbenchError("此脚本已保存，可试跑、立即签到或创建新录制")
    session.draft = await analyze_recording(session.captured, client)
    return {"draft": session.draft}
