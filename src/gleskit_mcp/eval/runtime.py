from __future__ import annotations

from contextlib import AsyncExitStack
import json
import os
from pathlib import Path
import re
import threading
from typing import Any

from gleskit_mcp.config import load_settings
from gleskit_mcp.workspace import clear_runtime_manifest, write_runtime_manifest

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
except Exception:  # pragma: no cover - optional dependency
    ClientSession = None
    StdioServerParameters = None
    stdio_client = None

try:
    from langchain_openai import ChatOpenAI
except Exception:  # pragma: no cover - optional dependency
    ChatOpenAI = None


def require_eval_runtime_dependencies() -> None:
    missing: list[str] = []
    if ClientSession is None or StdioServerParameters is None or stdio_client is None:
        missing.append("mcp")
    if ChatOpenAI is None:
        missing.append("langchain-openai")
    if missing:
        joined = ", ".join(sorted(set(missing)))
        raise RuntimeError(
            f"Missing optional evaluation dependencies: {joined}. Install them with `.venv/bin/python -m pip install -e \".[eval,mcp]\"`."
        )


def message_text(message: Any) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or item))
            else:
                text = getattr(item, "text", None)
                parts.append(text if text is not None else str(item))
        return "\n".join(part for part in parts if part)
    return str(content)


def compact_json(value: Any, max_chars: int = 8000) -> str:
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, indent=2, sort_keys=True, default=str)
        except TypeError:
            text = str(value)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n... [truncated]"


def tool_result_payload(result: Any) -> Any:
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return structured
    content = getattr(result, "content", None)
    if content:
        texts: list[str] = []
        for item in content:
            text = getattr(item, "text", None)
            if text is not None:
                texts.append(text)
            elif hasattr(item, "model_dump"):
                texts.append(compact_json(item.model_dump()))
            else:
                texts.append(str(item))
        joined = "\n".join(part for part in texts if part).strip()
        if joined:
            return joined
    if hasattr(result, "model_dump"):
        return result.model_dump()
    return str(result)


def deep_find_first(node: Any, keys: set[str]) -> Any:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in keys:
                return value
        for value in node.values():
            found = deep_find_first(value, keys)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = deep_find_first(item, keys)
            if found is not None:
                return found
    return None


def deep_find_all_lists(node: Any, keys: set[str]) -> list[Any]:
    found: list[Any] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in keys and isinstance(value, list):
                found.extend(value)
            found.extend(deep_find_all_lists(value, keys))
    elif isinstance(node, list):
        for item in node:
            found.extend(deep_find_all_lists(item, keys))
    return found


def mcp_tool_to_openai_schema(tool: Any) -> dict[str, Any]:
    parameters = tool.inputSchema or {"type": "object", "properties": {}}
    if parameters.get("type") != "object":
        parameters = {"type": "object", "properties": {"input": parameters}, "required": ["input"]}
    parameters.setdefault("properties", {})
    parameters.setdefault("required", [])
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or f"GLESKit tool: {tool.name}",
            "parameters": parameters,
        },
    }


def count_words(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text or ""))


def read_text_file(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def eval_assets_root() -> Path:
    return Path(__file__).resolve().parent / "assets"


def first_paragraph(content: str) -> str:
    for part in content.split("\n\n"):
        cleaned = part.strip()
        if cleaned:
            return cleaned
    return ""


def parse_skill_frontmatter(content: str) -> tuple[dict[str, str], str]:
    trimmed = (content or "").strip()
    if not trimmed.startswith("---\n"):
        return {}, trimmed
    parts = trimmed.split("---", 2)
    if len(parts) < 3:
        return {}, trimmed
    frontmatter_block = parts[1]
    body = parts[2].strip()
    frontmatter: dict[str, str] = {}
    for line in frontmatter_block.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        frontmatter[key.strip()] = value.strip()
    return frontmatter, body


def load_skill_bundles(skills_root: Path) -> list[dict[str, Any]]:
    skills: list[dict[str, Any]] = []
    for skill_dir in sorted([path for path in skills_root.iterdir() if path.is_dir()], key=lambda item: item.name):
        meta_path = skill_dir / ".gleskit-skill.json"
        meta: dict[str, Any] = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                meta = {}

        files: dict[str, str] = {}
        for md_path in sorted(skill_dir.rglob("*.md"), key=lambda item: item.as_posix()):
            rel = md_path.relative_to(skill_dir).as_posix()
            files[rel] = md_path.read_text(encoding="utf-8").strip()
        if not files:
            continue

        entrypoint_path = "SKILL.md" if "SKILL.md" in files else sorted(files.keys())[0]
        frontmatter, entrypoint_body = parse_skill_frontmatter(files[entrypoint_path])

        label = frontmatter.get("name") or str(meta.get("label") or "").strip() or skill_dir.name
        description = frontmatter.get("description") or first_paragraph(entrypoint_body) or label

        skills.append(
            {
                "skill_id": str(meta.get("skill_id") or skill_dir.name),
                "label": label,
                "description": description,
                "enabled": bool(meta.get("enabled", True)),
                "entrypoint_path": entrypoint_path,
                "entrypoint_body": entrypoint_body,
                "files": files,
            }
        )
    return skills


def select_active_skills(
    skills: list[dict[str, Any]],
    active_skill_ids: list[str] | None,
    *,
    skills_mode: bool,
) -> list[dict[str, Any]]:
    if not skills_mode:
        return []
    enabled = [skill for skill in skills if skill.get("enabled", True)]
    if active_skill_ids is None:
        return enabled
    active_set = {skill_id.strip() for skill_id in active_skill_ids if skill_id and skill_id.strip()}
    return [skill for skill in enabled if skill["skill_id"] in active_set or skill["label"] in active_set]


def build_workflow_skill_context(
    active_skills: list[dict[str, Any]],
    *,
    workflow_steps: list[Any] | tuple[Any, ...],
) -> str:
    sections: list[str] = []
    skill_by_id = {str(skill.get("skill_id") or ""): skill for skill in active_skills}
    primary_skill = skill_by_id.get("gleskit-research-assistant") or (active_skills[0] if active_skills else None)
    if primary_skill is None:
        return ""
    files = primary_skill.get("files") or {}
    lines = [
        f"Preloaded GLESKit skill plan: {primary_skill['label']} ({primary_skill['skill_id']})",
        "These skill references are mandatory and step-scoped for this evaluation. Do not decide whether to use them; apply them when the corresponding workflow step is active.",
    ]
    added_any = False
    for step in workflow_steps:
        required_files = [path for path in getattr(step, "required_skill_files", ()) if path in files]
        if not required_files:
            continue
        added_any = True
        lines.extend(
            [
                "",
                f"[Workflow step: {getattr(step, 'step_id', 'unknown')} - {getattr(step, 'title', 'Untitled')}]",
                "Mandatory skill references for this step:",
            ]
        )
        lines.extend(f"- {path}" for path in required_files)
        for path in required_files:
            content = str(files.get(path) or "").strip()
            if not content:
                continue
            lines.extend(["", f"[{path}]", content])
    if added_any:
        sections.append("\n".join(lines).strip())
    return "\n\n---\n\n".join(section for section in sections if section.strip())


def _strip_section_by_heading(text: str, heading: str, next_headings: list[str]) -> str:
    if not text.strip():
        return text
    start = text.find(heading)
    if start == -1:
        return text
    candidates = [text.find(next_heading, start + len(heading)) for next_heading in next_headings]
    candidates = [index for index in candidates if index != -1]
    end = min(candidates) if candidates else len(text)
    stripped = text[:start].rstrip() + "\n\n" + text[end:].lstrip()
    return stripped.strip() + "\n"


def sanitize_desktop_prompt_for_eval(system_prompt_text: str) -> str:
    text = str(system_prompt_text or "").strip()
    if not text:
        return ""
    text = _strip_section_by_heading(
        text,
        "Skill loading and reference-gating rules:",
        ["General GLESKit behavior:", "Tool discipline:", "Scientific grounding and assertion gate:"],
    )
    text = _strip_section_by_heading(
        text,
        "General GLESKit behavior:",
        ["Tool discipline:", "Scientific grounding and assertion gate:"],
    )
    text = text.replace(
        "3. active GLESKit skills that are available as internal on-demand tools",
        "3. optional GLESKit guidance layers that may or may not be active in this evaluation",
    )
    return text.strip()


def load_neutral_eval_prompt() -> str:
    return (eval_assets_root() / "neutral_eval_prompt.md").read_text(encoding="utf-8").strip()


def resolve_eval_base_prompt(
    *,
    prompt_mode: str,
    live_prompt_text: str,
    override_text: str,
) -> str:
    normalized = str(prompt_mode or "").strip().lower()
    if normalized == "off":
        return ""
    if normalized == "override":
        return str(override_text or "").strip()
    if normalized == "inherit_live":
        return sanitize_desktop_prompt_for_eval(live_prompt_text)
    if normalized == "neutral_eval":
        return load_neutral_eval_prompt()
    raise ValueError(f"Unsupported eval prompt mode: {prompt_mode}")


def compile_desktop_prompt(
    system_prompt_text: str,
    active_skills: list[dict[str, Any]],
    *,
    prompt_mode: bool,
    include_skill_catalog: bool = True,
) -> str:
    sections: list[str] = []
    if prompt_mode and system_prompt_text.strip():
        sections.append(system_prompt_text.strip())
    if active_skills and include_skill_catalog:
        sections.append(
            "\n".join(
                [
                    "GLESKit skills are available as on-demand internal tools.",
                    "Do not assume a skill is already loaded.",
                    "Load the relevant skill with `load_gleskit_skill` when its description matches the task.",
                    "After loading a skill, read only the supporting file you need with `read_gleskit_skill_file`.",
                    "Active skills for this run:",
                ]
            )
        )
        sections.append("\n".join(f"- {skill['label']}: {skill['description']}" for skill in active_skills))
    return "\n\n---\n\n".join(section for section in sections if section.strip())


def desktop_skill_tool_definitions(active_skills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not active_skills:
        return []
    return [
        {
            "type": "function",
            "function": {
                "name": "load_gleskit_skill",
                "description": "Load SKILL.md instructions for one active GLESKit skill before reading supporting files.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "skill_name": {"type": "string", "description": "Active skill label or skill_id"},
                    },
                    "required": ["skill_name"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_gleskit_skill_file",
                "description": "Read one supporting markdown file from a loaded skill.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "skill_name": {"type": "string", "description": "Active skill label or skill_id"},
                        "path": {"type": "string", "description": "Relative path to markdown file inside skill"},
                    },
                    "required": ["skill_name", "path"],
                    "additionalProperties": False,
                },
            },
        },
    ]


def resolve_skill_bundle(skill_name: str, active_skills: list[dict[str, Any]]) -> dict[str, Any] | None:
    cleaned = str(skill_name or "").strip().lower()
    if not cleaned:
        return None
    for skill in active_skills:
        if skill["skill_id"].lower() == cleaned or skill["label"].lower() == cleaned:
            return skill
    return None


def call_desktop_skill_tool(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    active_skills: list[dict[str, Any]],
    loaded_skill_ids: set[str],
) -> dict[str, Any]:
    if tool_name == "load_gleskit_skill":
        skill_name = str(arguments.get("skill_name") or "").strip()
        bundle = resolve_skill_bundle(skill_name, active_skills)
        if bundle is None:
            raise ValueError(f"Active GLESKit skill not found: {skill_name}")
        loaded_skill_ids.add(bundle["skill_id"])
        available_files = [path for path in sorted(bundle["files"].keys()) if not path.endswith("SKILL.md")]
        return {
            "skill_id": bundle["skill_id"],
            "skill_name": bundle["label"],
            "description": bundle["description"],
            "entrypoint": "SKILL.md",
            "content_markdown": bundle["entrypoint_body"],
            "available_files": available_files,
        }
    if tool_name == "read_gleskit_skill_file":
        skill_name = str(arguments.get("skill_name") or "").strip()
        path = str(arguments.get("path") or "").strip()
        if not path:
            raise ValueError("read_gleskit_skill_file requires a path")
        bundle = resolve_skill_bundle(skill_name, active_skills)
        if bundle is None:
            raise ValueError(f"Active GLESKit skill not found: {skill_name}")
        if bundle["skill_id"] not in loaded_skill_ids:
            raise ValueError(
                f"Load the '{bundle['label']}' skill with load_gleskit_skill before reading supporting files"
            )
        if path not in bundle["files"]:
            raise ValueError(f"Skill file not found in {bundle['label']}: {path}")
        return {
            "skill_id": bundle["skill_id"],
            "skill_name": bundle["label"],
            "path": path,
            "content_markdown": bundle["files"][path],
        }
    raise ValueError(f"Unsupported skill tool: {tool_name}")


def build_chat_model_from_provider(provider: dict[str, Any], *, model_id: str, temperature: float) -> Any:
    require_eval_runtime_dependencies()
    provider_kind = str(provider.get("kind") or "").strip().lower()
    endpoint = str(provider.get("endpoint") or "").strip() or "https://api.openai.com/v1"
    api_key = str(provider.get("api_key") or "").strip() or os.environ.get("OPENAI_API_KEY", "") or "not-needed"
    if not endpoint:
        raise RuntimeError(
            f"Provider '{provider.get('provider_id')}' is missing an endpoint, so the eval runner cannot construct a chat client."
        )
    return ChatOpenAI(
        model=model_id,
        temperature=temperature,
        api_key=api_key,
        base_url=endpoint,
    )


class RuntimeHeartbeat:
    def __init__(self, project_root: Path, workspace_root: Path, runtime_root: Path, interval_seconds: int = 10) -> None:
        previous_workspace_home = os.environ.get("GLESKIT_WORKSPACE_HOME")
        previous_runtime_home = os.environ.get("GLESKIT_RUNTIME_HOME")
        os.environ["GLESKIT_WORKSPACE_HOME"] = str(workspace_root)
        os.environ["GLESKIT_RUNTIME_HOME"] = str(runtime_root)
        try:
            self.settings = load_settings(project_root)
        finally:
            if previous_workspace_home is None:
                os.environ.pop("GLESKIT_WORKSPACE_HOME", None)
            else:
                os.environ["GLESKIT_WORKSPACE_HOME"] = previous_workspace_home
            if previous_runtime_home is None:
                os.environ.pop("GLESKIT_RUNTIME_HOME", None)
            else:
                os.environ["GLESKIT_RUNTIME_HOME"] = previous_runtime_home
        self.workspace_root = Path(workspace_root)
        self.runtime_root = Path(runtime_root)
        self.interval_seconds = interval_seconds
        self.state = {
            "active_session_id": None,
            "capture_enabled": True,
            "ui_name": "gleskit-eval",
        }
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def _write_once(self) -> None:
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        write_runtime_manifest(
            self.settings.runtime_manifest_path,
            workspace_root=self.workspace_root,
            active_session_id=self.state["active_session_id"],
            capture_enabled=self.state["capture_enabled"],
            ui_name=self.state["ui_name"],
        )

    def start(self) -> None:
        if self._thread is not None:
            return
        self._write_once()

        def _loop() -> None:
            while not self._stop_event.wait(self.interval_seconds):
                self._write_once()

        self._thread = threading.Thread(target=_loop, daemon=True)
        self._thread.start()

    def set_active_session(self, session_id: str | None) -> None:
        self.state["active_session_id"] = session_id
        self._write_once()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
        try:
            clear_runtime_manifest(self.settings.runtime_manifest_path)
        except FileNotFoundError:
            pass


class GLESKitRuntime:
    def __init__(self, *, project_root: Path, workspace_root: Path, runtime_root: Path) -> None:
        require_eval_runtime_dependencies()
        self.project_root = Path(project_root)
        self.workspace_root = Path(workspace_root)
        self.runtime_root = Path(runtime_root)
        self.heartbeat = RuntimeHeartbeat(self.project_root, self.workspace_root, self.runtime_root)
        self.exit_stack: AsyncExitStack | None = None
        self.session: Any | None = None
        self.tools: list[Any] = []
        self.openai_tools: list[dict[str, Any]] = []

    async def start(self) -> "GLESKitRuntime":
        if self.session is not None:
            return self
        self.exit_stack = AsyncExitStack()
        server_params = StdioServerParameters(
            command=str(self.project_root / ".venv" / "bin" / "python"),
            args=[
                "-m",
                "gleskit_mcp",
                "--project-root",
                str(self.project_root),
                "serve",
                "--transport",
                "stdio",
            ],
            env={
                "PYTHONPATH": str(self.project_root / "src"),
                "PATH": os.environ.get("PATH", ""),
                "GLESKIT_WORKSPACE_HOME": str(self.workspace_root),
                "GLESKIT_RUNTIME_HOME": str(self.runtime_root),
            },
            cwd=str(self.project_root),
        )
        read_stream, write_stream = await self.exit_stack.enter_async_context(stdio_client(server_params))
        self.session = await self.exit_stack.enter_async_context(ClientSession(read_stream, write_stream))
        await self.session.initialize()
        self.tools = (await self.session.list_tools()).tools
        self.openai_tools = [mcp_tool_to_openai_schema(tool) for tool in self.tools]
        self.heartbeat.start()
        return self

    async def shutdown(self) -> None:
        self.heartbeat.stop()
        if self.exit_stack is not None:
            await self.exit_stack.aclose()
        self.exit_stack = None
        self.session = None
        self.tools = []
        self.openai_tools = []

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        if self.session is None:
            raise RuntimeError("MCP runtime is not started.")
        return await self.session.call_tool(name, arguments or {})
