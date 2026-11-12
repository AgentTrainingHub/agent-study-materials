"""W04 的多轮消息语法；只校验，不修补或改写消息。"""
from w02_support import MAX_BATCH_CALLS, ProtocolError


def validate_messages(messages, *, mode="prefix", followup_user_indices=()):
    """prefix 允许正在保存的合法尾；request/complete 要求对应边界。"""
    def reject(message):
        raise ProtocolError("invalid_transcript", message)

    if mode not in {"prefix", "request", "complete"}:
        raise ValueError("unknown validation mode")
    if (not isinstance(followup_user_indices, (list, tuple, set, frozenset))
            or any(type(index) is not int or index < 1 for index in followup_user_indices)):
        reject("人工继续的 user 位置必须为正整数。")
    allowed_users = set(followup_user_indices)
    if not isinstance(messages, list) or not messages:
        reject("消息必须以 system 开始。")
    first = messages[0]
    if (not isinstance(first, dict) or set(first) != {"role", "content"}
            or first.get("role") != "system" or not isinstance(first.get("content"), str)
            or not first["content"].strip()):
        reject("system 消息字段无效。")
    state, pending = "user", set()
    for index, message in enumerate(messages[1:], 1):
        if not isinstance(message, dict):
            reject(f"消息 {index} 必须为对象。")
        role = message.get("role")
        if role == "user":
            continuation = (state == "assistant" and index in allowed_users
                            and messages[index - 1].get("role") == "tool")
            if (state != "user" and not continuation) or set(message) != {"role", "content"}:
                reject(f"消息 {index} 的 user 位置或字段无效。")
            if not isinstance(message["content"], str) or not message["content"].strip():
                reject("user 内容不能为空。")
            state = "assistant"
        elif role == "assistant":
            if state != "assistant" or set(message) - {"role", "content", "tool_calls"}:
                reject(f"消息 {index} 的 assistant 位置或字段无效。")
            calls, content = message.get("tool_calls"), message.get("content")
            if calls is None:
                if set(message) != {"role", "content"} or not isinstance(content, str) or not content.strip():
                    reject("最终 assistant 必须含非空文本。")
                state = "user"
            else:
                if (not isinstance(calls, list) or not 1 <= len(calls) <= MAX_BATCH_CALLS
                        or (content is not None and not isinstance(content, str))):
                    reject("工具批次字段无效。")
                pending = set()
                for call in calls:
                    if not isinstance(call, dict) or set(call) != {"id", "type", "function"}:
                        reject("工具调用字段无效。")
                    ident, fn = call["id"], call["function"]
                    if not isinstance(ident, str) or not ident.strip() or ident in pending:
                        reject("同批 call_id 必须非空且唯一。")
                    if (call["type"] != "function" or not isinstance(fn, dict)
                            or set(fn) != {"name", "arguments"}
                            or not isinstance(fn["name"], str) or not fn["name"].strip()
                            or not isinstance(fn["arguments"], str)):
                        reject("工具 function 字段无效。")
                    pending.add(ident)
                state = "tool"
        elif role == "tool":
            if state != "tool" or set(message) != {"role", "tool_call_id", "content"}:
                reject(f"消息 {index} 的 tool 位置或字段无效。")
            ident = message["tool_call_id"]
            if not isinstance(ident, str) or ident not in pending:
                reject("工具结果找不到本批未完成的调用。")
            if not isinstance(message["content"], str) or not message["content"].strip():
                reject("工具结果内容不能为空。")
            pending.remove(ident)
            if not pending:
                state = "assistant"
        else:
            reject(f"消息 {index} 的 role 无效。")
    if mode == "request" and (state != "assistant" or len(messages) < 2):
        reject("请求必须位于 user 或已配齐工具结果之后。")
    if mode == "complete" and (state != "user" or len(messages) < 3):
        reject("完整轮次必须由最终 assistant 结束。")
    if any(index >= len(messages) or messages[index].get("role") != "user" for index in allowed_users):
        reject("人工继续位置必须对应当前投影中的真实 user。")


def validate_transcript(messages):
    validate_messages(messages, mode="request")
