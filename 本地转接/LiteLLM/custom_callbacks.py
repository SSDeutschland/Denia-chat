# custom_callbacks.py - LiteLLM proxy hook
# ① HoistToolResultImages：CC Read 读图会把图片放在 tool_result 块里，LiteLLM 的
#    anthropic->openai adapter 会把单图 tool_result 翻成 tool 消息里的 base64
#    data-URL 纯文本串（模型看不到图还白烧 token）。此钩子把图搬进紧随的 user
#    消息（OpenAI 视觉路径可用，已验证）。运行在 Anthropic 层、翻译之前。
# ② 纯文本模型兜底（2026-08-09 实锤后补）：桥接的纯文本模型组（deepseek/glm）
#    收到图片块会被上游 400（glm=1210 content.type，deepseek=invalid_request），
#    且图片留在历史里会毒化之后每一轮（达妮娅生图后 Read 回看触发过）。
#    对这些模型：图片块→占位文字；同角色连续消息合并——deepseek 系严格要求
#    tool_calls 消息后面紧邻 tool 输出，续接会话从 transcript 重建历史时并行
#    tool_use 是分开的 assistant 条目，不合并则报 "No tool output found"。
from litellm.integrations.custom_logger import CustomLogger

NOTE_IN_TOOL = "[tool returned image(s), moved to the next user message]"
LEAD_TEXT = "[image(s) from the previous tool result]"

# 桥上的纯文本模型组（别名级；新增纯文本别名时往这里加）
TEXT_ONLY_MODELS = {"deepseek-v4-flash", "deepseek-v4-pro", "glm-5.2"}
IMG_PLACEHOLDER = "[此处有一张图片，当前模型不可见]"


def _is_text_only(model):
    """data['model'] 是请求别名（路由在 hook 之后），取末段匹配。"""
    if not model:
        return False
    return str(model).split("/")[-1] in TEXT_ONLY_MODELS


def _strip_images(messages):
    """所有 content 列表里的 image 块→占位文字（含 tool_result 内层）。"""
    changed = False
    for msg in messages:
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        new_blocks = []
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") == "image":
                new_blocks.append({"type": "text", "text": IMG_PLACEHOLDER})
                changed = True
            elif isinstance(blk, dict) and blk.get("type") == "tool_result":
                tr = blk.get("content")
                if isinstance(tr, list) and any(
                        isinstance(x, dict) and x.get("type") == "image" for x in tr):
                    keep = [{"type": "text", "text": IMG_PLACEHOLDER}
                            if isinstance(x, dict) and x.get("type") == "image" else x
                            for x in tr]
                    blk = {**blk, "content": keep}
                    changed = True
                new_blocks.append(blk)
            else:
                new_blocks.append(blk)
        msg["content"] = new_blocks
    return changed


def _as_blocks(content):
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    return list(content) if isinstance(content, list) else []


def _merge_same_role(messages):
    """合并同角色连续消息（修 deepseek 对 tool_calls 紧邻性的校验）。
    Anthropic 协议下同角色合并天然合法；tool_result 仍保持 user 消息首个块。"""
    out = []
    for msg in messages:
        if (out and isinstance(msg, dict)
                and msg.get("role") == out[-1].get("role")
                and msg.get("role") in ("user", "assistant")):
            prev = out[-1]
            prev["content"] = _as_blocks(prev.get("content")) \
                + _as_blocks(msg.get("content"))
        else:
            out.append(dict(msg) if isinstance(msg, dict) else msg)
    return out


class HoistToolResultImages(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        messages = data.get("messages")
        if not isinstance(messages, list):
            return data
        if _is_text_only(data.get("model")):
            # 纯文本模型：不 hoist（搬了也看不见），图片→占位文字 + 同角色合并
            _strip_images(messages)
            data["messages"] = _merge_same_role(messages)
            return data
        out = []
        changed = False
        for msg in messages:
            content = msg.get("content") if isinstance(msg, dict) else None
            if not (isinstance(msg, dict) and msg.get("role") == "user"
                    and isinstance(content, list)):
                out.append(msg)
                continue
            imgs = []
            new_blocks = []
            for blk in content:
                if isinstance(blk, dict) and blk.get("type") == "tool_result":
                    tr = blk.get("content")
                    if isinstance(tr, list) and any(
                            isinstance(x, dict) and x.get("type") == "image" for x in tr):
                        keep = [x for x in tr
                                if not (isinstance(x, dict) and x.get("type") == "image")]
                        imgs.extend(x for x in tr
                                    if isinstance(x, dict) and x.get("type") == "image")
                        keep.append({"type": "text", "text": NOTE_IN_TOOL})
                        blk = {**blk, "content": keep}
                        changed = True
                new_blocks.append(blk)
            out.append({**msg, "content": new_blocks})
            if imgs:
                out.append({"role": "user",
                            "content": [{"type": "text", "text": LEAD_TEXT}] + imgs})
        if changed:
            data["messages"] = out
        return data


proxy_handler_instance = HoistToolResultImages()
