"""Natural-language drafts for safe Skill and MCP configuration.

The model is only a draft generator. Validation and persistence stay in the
existing Hub and MCP services so a prompt can never write arbitrary files,
start a process, or persist a credential.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..agent import llm
from ..integrations import mcp_settings
from . import registry


KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
SECRET_RE = re.compile(r"(api[_-]?key|token|password|secret|cookie|credential)", re.I)
INLINE_EXEC_RE = re.compile(r"(^|\n)\s*(python|python3|bash|sh|zsh|powershell|node)\s+", re.I)

SKILL_FIELDS = {"key", "name", "version", "description", "content", "enabled"}
MCP_FIELDS = {
    "id", "label", "transport", "url", "command", "args", "workspace",
    "course", "network", "tools", "env_names", "bearer_env", "enabled", "trusted",
}
MCP_TRANSPORTS = {"stdio", "streamable_http"}


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} 必须是对象")
    return value


def _key(value: Any, label: str) -> str:
    if not isinstance(value, str) or not KEY_RE.fullmatch(value.strip()):
        raise ValueError(f"{label} 只能包含字母、数字、下划线和短横线")
    return value.strip()


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} 不能为空")
    return value.strip()


def _reject_secrets(value: Any, path: str = "draft") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if SECRET_RE.search(str(key)):
                raise ValueError("自然语言配置不能接收密钥或凭据值")
            _reject_secrets(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_secrets(child, f"{path}[{index}]")
    elif isinstance(value, str) and SECRET_RE.search(value):
        # Do not reject ordinary prose such as “不保存 token”；only values
        # that look like a credential assignment are unsafe to carry forward.
        if re.search(r"(?:=|:|：)\s*\S+", value):
            raise ValueError("自然语言配置不能接收密钥或凭据值")


def _skill_draft(raw: Any) -> dict[str, Any]:
    draft = _object(raw, "Skill 草稿")
    unknown = set(draft) - SKILL_FIELDS
    if unknown:
        raise ValueError(f"Skill 草稿包含不支持字段：{sorted(unknown)[0]}")
    content = _required_text(draft.get("content"), "Skill 内容")
    if INLINE_EXEC_RE.search(content):
        raise ValueError("Skill 草稿不能包含直接执行 Shell 的内联指令")
    return {
        "key": _key(draft.get("key"), "Skill 标识"),
        "name": _required_text(draft.get("name"), "Skill 名称"),
        "version": _required_text(draft.get("version", "1.0.0"), "Skill 版本"),
        "description": str(draft.get("description") or "").strip(),
        "content": content,
        "enabled": True,
    }


def _env_names(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("MCP 环境变量只能填写变量名数组")
    result = []
    for item in value:
        name = item.strip()
        if not ENV_RE.fullmatch(name):
            raise ValueError("MCP 环境变量只能填写大写变量名，不能填写变量值")
        if name not in result:
            result.append(name)
    return result


def _mcp_tools(value: Any) -> dict[str, dict[str, Any]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("MCP 工具授权必须是对象")
    result = {}
    for name, config in value.items():
        if not isinstance(name, str) or not KEY_RE.fullmatch(name):
            raise ValueError("MCP 工具名无效")
        config = _object(config, f"MCP 工具 {name}")
        if set(config) - {"read_only", "description"}:
            raise ValueError("MCP 只允许配置只读工具")
        if config.get("read_only") is not True:
            raise ValueError("MCP 只允许授权只读工具")
        result[name] = {"read_only": True}
        if config.get("description"):
            result[name]["description"] = str(config["description"])[:400]
    return result


def _mcp_draft(raw: Any) -> dict[str, Any]:
    draft = _object(raw, "MCP 草稿")
    unknown = set(draft) - MCP_FIELDS
    if unknown:
        raise ValueError(f"MCP 草稿包含不支持字段：{sorted(unknown)[0]}")
    transport = draft.get("transport", "streamable_http")
    if transport not in MCP_TRANSPORTS:
        raise ValueError("MCP 传输方式只能是 stdio 或 streamable_http")
    result: dict[str, Any] = {
        "id": _key(draft.get("id"), "MCP 服务 ID"),
        "label": _required_text(draft.get("label"), "MCP 服务名称"),
        "transport": transport,
        "workspace": _key(draft.get("workspace", "studypal"), "工作区"),
        "course": draft.get("course") or None,
        "network": bool(draft.get("network", transport == "streamable_http")),
        "tools": _mcp_tools(draft.get("tools")),
        "env_names": _env_names(draft.get("env_names")),
        # Natural-language creation is always a disabled, untrusted draft.
        "enabled": False,
        "trusted": False,
    }
    if transport == "streamable_http":
        result["url"] = _required_text(draft.get("url"), "MCP 服务 URL")
        if not result["url"].startswith(("http://", "https://")):
            raise ValueError("MCP 服务 URL 必须是 http 或 https 地址")
        result["bearer_env"] = result["env_names"][0] if result["env_names"] else ""
    else:
        result["command"] = _required_text(draft.get("command"), "MCP 启动命令")
        args = draft.get("args", [])
        if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
            raise ValueError("MCP 启动参数必须是字符串数组")
        result["args"] = args[:32]
    if result["course"] is not None and not isinstance(result["course"], str):
        raise ValueError("MCP 课程必须是字符串")
    return result


def _normalize(target: str, draft: Any) -> dict[str, Any]:
    _reject_secrets(draft)
    if target == "skill":
        return _skill_draft(draft)
    if target == "mcp":
        return _mcp_draft(draft)
    raise ValueError("配置目标只能是 skill 或 mcp")


def validate_preview(payload: Any) -> dict[str, Any]:
    payload = _object(payload, "配置预览")
    target = payload.get("target")
    if target not in {"skill", "mcp"}:
        raise ValueError("配置目标只能是 skill 或 mcp")
    draft = _normalize(target, payload.get("draft"))
    result = {
        "target": target,
        "draft": draft,
        "missing": list(payload.get("missing") or []),
        "warnings": list(payload.get("warnings") or []),
        "requires_confirmation": True,
    }
    if target == "mcp":
        result["base_revision"] = payload.get("base_revision") or mcp_settings.read()["revision"]
    return result


def _model_prompt(target: str, prompt: str) -> list[dict[str, str]]:
    schema = (
        '{"target":"skill","draft":{"key":"...","name":"...",'
        '"version":"1.0.0","description":"...","content":"# ..."}}'
        if target == "skill" else
        '{"target":"mcp","draft":{"id":"...","label":"...",'
        '"transport":"streamable_http","url":"https://...",'
        '"workspace":"studypal","network":true,"tools":{"search":{"read_only":true}},'
        '"env_names":["SERVICE_TOKEN"]}}'
    )
    return [
        {"role": "system", "content": (
            "你是本地桌面应用的配置草稿助手。只输出 JSON，不要 Markdown。"
            "不得输出密钥、凭据值、任意文件路径、Shell 脚本或写入/删除工具。"
            f"目标是 {target}，严格参考这个最小结构：{schema}"
        )},
        {"role": "user", "content": prompt.strip()[:4000]},
    ]


def preview(prompt: str, *, target: str) -> dict[str, Any]:
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("请先描述要创建的能力")
    if target not in {"skill", "mcp"}:
        raise ValueError("配置目标只能是 skill 或 mcp")
    raw = llm.chat("fast", _model_prompt(target, prompt), temperature=0,
                   response_format={"type": "json_object"})
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("模型没有返回有效的配置 JSON") from exc
    payload = _object(payload, "模型配置结果")
    if payload.get("target") not in (None, target):
        raise ValueError("模型返回的配置目标与当前页面不一致")
    result = validate_preview({"target": target, "draft": payload.get("draft")})
    result["warnings"] = [
        "生成结果只是预览，保存前请检查名称、权限和来源。",
    ]
    return result


def confirm(payload: Any) -> dict[str, Any]:
    preview_payload = validate_preview(payload)
    target = preview_payload["target"]
    draft = dict(preview_payload["draft"])
    if target == "skill":
        created = registry.create_skill(draft)
    else:
        current = mcp_settings.read()
        if preview_payload.get("base_revision") != current["revision"]:
            raise ValueError("MCP 配置已变化，请重新生成预览")
        servers = [server for server in current["servers"] if server["id"] != draft["id"]]
        servers.append(draft)
        created = mcp_settings.save(servers, current["revision"])
    return {"ok": True, "target": target, "created": created, "next": "请按需绑定到 Agent" if target == "skill" else "请先在 MCP 页面检查并启用服务"}
