from __future__ import annotations

import inspect
from dataclasses import replace
import os
from pathlib import Path
import re
import shutil
import sys
import uuid

from gleskit_mcp.store import WorkspaceStore
from gleskit_mcp.workspace import WorkspaceService
from gleskit_mcp.reference_data import ReferenceCatalog
from .chat import Chat
from .common import WebError, identifier, read_json, write_private
from .engine import Engine
from .files import Files
from .jobs import Jobs
from .providers import Providers
from .runtime import Runtime


WORKSPACE_COMMANDS = {
    "list_sessions": "list_sessions", "create_session": "create_session", "update_session": "update_session",
    "get_session_summary": "get_session_summary", "list_session_notes": "list_notes", "get_session_note": "get_note",
    "create_session_note": "add_note", "update_session_note": "update_note", "delete_session_notes": "delete_notes",
    "list_session_reports": "list_reports", "get_session_report": "get_session_report", "delete_session_reports": "delete_reports",
    "list_session_artifacts": "list_artifacts", "list_session_uploads": "list_uploads",
    "get_artifact": "get_artifact", "get_upload": "get_upload", "delete_session_artifacts": "delete_artifacts",
    "delete_session_uploads": "delete_uploads", "list_cohorts": "list_cohorts", "list_cohort_providers": "list_cohort_providers",
    "cohort_catalog": "list_cohort_catalog", "get_cohort": "get_cohort", "update_cohort": "update_cohort",
    "delete_cohort": "delete_cohort", "create_session_from_cohort": "create_session_from_cohort",
    "create_session_from_artifacts": "create_session_from_artifacts", "archive_session": "archive_session",
}
READ_COMMANDS = {name for name in WORKSPACE_COMMANDS if name.startswith(("list_", "get_"))} | {
    "cohort_catalog", "bootstrap_app", "bootstrap_workspace", "reference_status", "get_session_snapshot",
    "get_chat_conversation", "list_chat_providers", "get_provider_catalog", "fetch_provider_models", "get_cohort_download_job", "get_web_status", "get_web_settings"}
WEB_COMMANDS = {"bootstrap_app", "bootstrap_workspace", "reference_status", "update_references", "get_session_snapshot",
    "delete_sessions", "create_session_upload", "create_cohort", "start_cohort_download_job", "get_cohort_download_job",
    "cancel_cohort_download_job", "write_runtime_heartbeat", "clear_runtime_manifest", "bootstrap_chat",
    "reconnect_chat_runtime", "create_chat_conversation", "get_chat_conversation", "rename_chat_conversation",
    "delete_chat_conversation", "list_chat_providers", "save_chat_provider", "delete_chat_provider", "send_chat_message",
    "cancel_chat_request", "resolve_chat_tool_approval", "reset_chat_conversation_tool_approval", "refresh_chat_model_catalog",
    "fetch_link_preview", "get_web_status", "get_web_settings", "save_web_settings", "start_web_engine", "stop_web_engine",
    "browse_existing_workspace", "use_web_workspace", "use_configured_workspace", "get_provider_catalog", "fetch_provider_models"}
ALL_COMMANDS = set(WORKSPACE_COMMANDS) | WEB_COMMANDS
WEB_FIELDS = {
    **{name: set() for name in WEB_COMMANDS},
    "get_session_snapshot": {"sessionId"}, "delete_sessions": {"sessionIds"},
    "create_session_upload": {"sessionId", "sourceMode", "fileName", "mimeType", "dataB64", "sourcePath", "title", "stagingId"},
    "create_cohort": {"title", "transpose", "sampleColumn", "groupColumn", "minCount", "countsSourceMode", "metadataSourceMode",
                      "countsDataB64", "metadataDataB64", "countsFileName", "metadataFileName", "countsSourcePath", "metadataSourcePath",
                      "sourceCountsPath", "sourceMetadataPath", "tags", "countsStagingId", "metadataStagingId"},
    "start_cohort_download_job": {"title", "providerId", "datasetId", "tissue", "sampleLimit", "tags"},
    "get_cohort_download_job": {"jobId"}, "cancel_cohort_download_job": {"jobId"},
    "write_runtime_heartbeat": {"activeSessionId", "captureEnabled"}, "create_chat_conversation": {"title"},
    "get_chat_conversation": {"conversationId"}, "rename_chat_conversation": {"conversationId", "title"},
    "delete_chat_conversation": {"conversationId"}, "delete_chat_provider": {"providerId"},
    "save_chat_provider": {"providerId", "kind", "label", "endpoint", "apiKey", "keySource", "models", "defaultModel", "enabled"},
    "send_chat_message": {"conversationId", "providerId", "modelId", "agentId", "content", "pipelineMode", "inputDataRefs"},
    "cancel_chat_request": {"conversationId"}, "resolve_chat_tool_approval": {"approvalId", "action"},
    "reset_chat_conversation_tool_approval": {"conversationId"}, "save_web_settings": {"extraInstructions"},
    "fetch_link_preview": {"url"},
    "fetch_provider_models": {"providerId", "kind", "endpoint", "apiKey", "keySource", "force", "includeMetadata"},
}


def snake(name):
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def validate_ids(values, workspace):
    for key, value in values.items():
        if key.endswith("Id") and key != "modelId" and value is not None:
            identifier(value)
        elif key.endswith("Ids") or key == "inputDataRefs":
            if not isinstance(value, list) or len(value) > 1000:
                raise WebError("Invalid identifier list.")
            for item in value:
                identifier(item)
        if key in {"sessionId", "cohortId"} and value:
            parent = workspace / ("sessions" if key == "sessionId" else "cohorts")
            path = parent / value
            if any(part.is_symlink() for part in (path, *path.parents)) or not path.resolve().is_relative_to(parent.resolve()):
                raise WebError("Unsafe workspace path.", 403)
        if key == "sources":
            if not isinstance(value, list):
                raise WebError("Invalid artifact sources.")
            for source in value:
                if not isinstance(source, dict) or set(source) != {"session_id", "artifact_id"}:
                    raise WebError("Each artifact source requires session_id and artifact_id.")
                validate_ids({"sessionId": source["session_id"], "artifactId": source["artifact_id"]}, workspace)
        if key in {"captureEnabled", "enabled", "pipelineMode", "transpose"} and type(value) is not bool:
            raise WebError("Invalid boolean field: " + key)
        if key == "tags" and (not isinstance(value, list) or any(not isinstance(tag, str) or len(tag) > 200 for tag in value)):
            raise WebError("Tags must be a list of short strings.")
        if isinstance(value, str) and ("\0" in value or len(value) > 400000):
            raise WebError("Invalid or oversized text field.")
        if key in {"title", "description", "bodyMarkdown", "species", "author", "query", "content", "kind", "label", "modelId", "agentId"} and value is not None and not isinstance(value, str):
            raise WebError("Expected a text field: " + key)
        if key in {"minCount", "sampleLimit"} and (type(value) is not int or value < 0):
            raise WebError("Expected a nonnegative integer: " + key)


class Commands:
    def __init__(self, profile, credentials=None, engine_factory=Engine):
        self.profile = profile
        self.workspace = self.open_workspace(profile.workspace, profile.settings)
        self.read_only = profile.workspace_source == "configured-read-only"
        self.providers = Providers(profile, credentials)
        self.engine = engine_factory(profile, self.providers)
        self.chat = Chat(profile, self.engine)
        self.files = Files(profile)
        self.jobs = Jobs(profile)
        self.runtime = Runtime(profile)
        self.settings_path = profile.data / "web-settings.json"

    @staticmethod
    def open_workspace(root, settings):
        if (root / "sessions").is_dir():
            # Opening an existing workspace must not initialize directories or
            # rewrite records. The unchanged store handles explicit later edits.
            store = WorkspaceStore.__new__(WorkspaceStore)
            store.workspace_root = root.resolve()
            store.sessions_root, store.reports_root, store.cohorts_root = (root / part for part in ("sessions", "reports", "cohorts"))
        else:
            store = WorkspaceStore(root)
        return WorkspaceService(store, settings=settings)

    def settings(self):
        return read_json(self.settings_path, {"extra_instructions": ""})

    def status(self):
        import importlib.util
        return {"engine": self.engine.status(), "runtime": self.runtime.snapshot(),
                "mcp": self.engine.mcp_status(), "credential_boundary": "python-loopback-relay",
                "profile": str(self.profile.data), "workspace": str(self.profile.workspace),
                "workspace_source": self.profile.workspace_source,
                "browsing_read_only": self.read_only, "existing_workspace_available": (self.profile.existing_workspace / "sessions").is_dir(),
                "credential_storage": self.providers.credentials.storage,
                "python": sys.version.split()[0], "rscript_available": bool(shutil.which(self.profile.settings.rscript_bin)),
                "r_library_available": self.profile.settings.r_lib_root.is_dir(),
                "mcp_available": importlib.util.find_spec("mcp") is not None,
                "active_turns": len(self.chat.active), "active_jobs": sum(row["can_cancel"] for row in self.jobs.rows.values()),
                "release_ready": False, "engine_privacy_warning": "This web shell keeps provider keys in Python and filters provider responses through a local relay before OpenCode receives them. Existing native-engine files are not cleaned or changed. Public release still requires source-history and native-runtime privacy review."}

    def execute(self, command, values, tab, expected=None):
        if command not in ALL_COMMANDS:
            raise WebError("Unknown web command.", 404)
        if not isinstance(values, dict):
            raise WebError("Command arguments must be an object.")
        if command in WEB_FIELDS and set(values) - WEB_FIELDS[command]:
            raise WebError("Unknown arguments for this web command.")
        validate_ids(values, self.workspace.store.workspace_root)
        if command not in READ_COMMANDS or command == "fetch_provider_models":
            self.runtime.require(tab)
        v, w = values, self.workspace
        if self.read_only:
            if command == "write_runtime_heartbeat":
                return {**self.runtime.snapshot(), "available": False, "active_session_id": None, "capture_enabled": False}
            if (command in WORKSPACE_COMMANDS and command not in READ_COMMANDS) or command in {
                "delete_sessions", "create_session_upload", "create_cohort", "start_cohort_download_job"}:
                raise WebError("This existing workspace is read-only. Switch back to the separate web workspace in Settings to create or change saved work.", 403)
        if command in WORKSPACE_COMMANDS:
            fn = getattr(w, WORKSPACE_COMMANDS[command])
            args = {snake(key): value for key, value in v.items()}
            params = inspect.signature(fn).parameters
            if set(args) - set(params):
                raise WebError("Unknown arguments for this command.")
            try:
                inspect.signature(fn).bind(**args)
            except TypeError:
                raise WebError("Missing command arguments.") from None
            result = fn(**args)
            return self.files.enrich_artifact(result) if command == "get_artifact" else result
        if command in {"bootstrap_app", "bootstrap_workspace"}:
            result = {"paths": {"standalone": True, "web": True, "project_root": str(self.profile.data),
                                "workspace_root": str(w.store.workspace_root), "read_only": self.read_only,
                                "workspace_source": self.profile.workspace_source,
                                "reference_root": str(self.profile.settings.reference_root),
                                "cli_path": sys.executable}, "runtime": self.runtime.snapshot(), "sessions": w.list_sessions()}
            if command == "bootstrap_app":
                result["reference_status"] = ReferenceCatalog(self.profile.settings).status()
            return result
        if command == "get_session_snapshot":
            session = v["sessionId"]
            return {"session_id": session, "summary": w.get_session_summary(session),
                    "artifacts": w.list_artifacts(session), "uploads": w.list_uploads(session)}
        if command == "reference_status":
            return ReferenceCatalog(self.profile.settings).status()
        if command == "update_references":
            return ReferenceCatalog(self.profile.settings).refresh_all()
        if command == "delete_sessions":
            if self.runtime.active in v["sessionIds"]:
                self.runtime.select(tab, None, False)
            return w.delete_sessions(v["sessionIds"])
        if command == "write_runtime_heartbeat":
            session = v.get("activeSessionId")
            if session:
                w.store.get_session(session)
            return self.runtime.select(tab, session, v.get("captureEnabled", False), expected)
        if command == "clear_runtime_manifest":
            return self.runtime.select(tab, None, False, expected)
        if command == "create_session_upload":
            return self._upload(v)
        if command == "create_cohort":
            return self._cohort(v)
        if command == "start_cohort_download_job":
            return self.jobs.start(**v)
        if command == "get_cohort_download_job":
            return self.jobs.get(v["jobId"])
        if command == "cancel_cohort_download_job":
            return self.jobs.cancel(v["jobId"])
        if command == "list_chat_providers":
            return self.providers.list()
        if command == "get_provider_catalog":
            return self.providers.catalog()
        if command == "fetch_provider_models":
            return self.providers.fetch_models(v.get("providerId"), kind=v.get("kind"), endpoint=v.get("endpoint"), api_key=v.get("apiKey"), key_source=v.get("keySource"), force=v.get("force", False), include_metadata=v.get("includeMetadata", False))
        if command in {"save_chat_provider", "delete_chat_provider"}:
            self.chat.idle()
            before = self.providers.active()
            result = self.providers.save(**v) if command == "save_chat_provider" else self.providers.delete(v["providerId"])
            if self.engine.status()["running"] and (before != self.providers.active() or v.get("apiKey") or v.get("keySource")):
                self.engine.stop()
            return result
        if command == "bootstrap_chat":
            return self.chat.bootstrap()
        if command == "create_chat_conversation":
            return self.chat.create(v.get("title"))
        if command == "get_chat_conversation":
            return self.chat.detail(v["conversationId"])
        if command == "rename_chat_conversation":
            return self.chat.rename(v["conversationId"], v["title"])
        if command == "delete_chat_conversation":
            return self.chat.delete(v["conversationId"])
        if command == "send_chat_message":
            return self.chat.send(v["conversationId"], v["providerId"], v["modelId"], v["agentId"], v["content"],
                                  self.runtime.snapshot(), pipeline=v.get("pipelineMode", False), refs=v.get("inputDataRefs", []),
                                  extra=self.settings()["extra_instructions"])
        if command == "cancel_chat_request":
            return self.chat.cancel(v["conversationId"])
        if command == "resolve_chat_tool_approval":
            return self.chat.approve(v["approvalId"], v["action"])
        if command == "reset_chat_conversation_tool_approval":
            return self.chat.reset_approval(v["conversationId"])
        if command in {"reconnect_chat_runtime", "start_web_engine", "stop_web_engine"}:
            self.chat.idle()
            if command != "start_web_engine":
                self.engine.stop()
            return self.engine.start() if command != "stop_web_engine" else self.engine.status()
        if command == "refresh_chat_model_catalog":
            raise WebError("Global OpenCode catalog downloads are disabled. Use Settings & system to search or refresh the selected provider's models.")
        if command == "fetch_link_preview":
            raise WebError("Automatic external link previews are disabled in the local browser. Open the link explicitly.")
        if command == "get_web_status":
            return self.status()
        if command == "get_web_settings":
            return {**self.settings(), "managed_config": self.engine.configuration()}
        if command == "save_web_settings":
            self.chat.idle()
            text = v.get("extraInstructions")
            if set(v) != {"extraInstructions"} or not isinstance(text, str) or len(text) > 32000:
                raise WebError("Enter at most 32,000 characters of additional instructions.")
            write_private(self.settings_path, {"extra_instructions": text})
            return {"saved": True}
        if command in {"browse_existing_workspace", "use_web_workspace", "use_configured_workspace"}:
            self.chat.idle()
            if any(row["can_cancel"] for row in self.jobs.rows.values()):
                raise WebError("Finish or cancel the active download before switching workspaces.", 409)
            source = {"browse_existing_workspace": "configured-read-only", "use_web_workspace": "separate",
                      "use_configured_workspace": "configured"}[command]
            root = self.profile.workspace_path(source)
            workspace = self.open_workspace(root, replace(self.profile.settings, workspace_default_root=root))
            self.runtime.select(tab, None, False)
            # MCP's workspace environment is fixed at launch. Stop the owned
            # engine once, explicitly, so it cannot capture into the old root.
            self.engine.stop()
            self.profile.select_workspace(source)
            self.workspace, self.read_only = workspace, source == "configured-read-only"
            self.files.read_roots.add(root)
            self.runtime.select(tab, None, False)
            return {"read_only": self.read_only, "workspace_source": source}
        raise WebError("Command is not implemented.", 404)

    def _upload(self, v):
        session = v["sessionId"]
        self.workspace.store.get_session(session)
        source, copied = self.files.input(v)
        target = source
        try:
            if copied:
                target = self.files.copy_into(source, self.profile.workspace / "sessions" / session / "uploads" / ("upload_" + uuid.uuid4().hex), v.get("fileName"))
            return self.workspace.create_upload(session, data_path=str(target), source_mode="copied" if copied else "linked",
                        title=v.get("title") or "", source_path=str(source) if not copied else "", stored_path=str(target) if copied else "",
                        file_name=v.get("fileName") or target.name, mime_type=v.get("mimeType") or "")
        finally:
            if copied:
                source.unlink(missing_ok=True)

    def _cohort(self, v):
        counts, copied_counts = self.files.input(v, "counts")
        metadata, copied_metadata = None, False
        try:
            metadata, copied_metadata = self.files.input(v, "metadata")
            return self.workspace.create_cohort(title=v["title"], counts_path=str(counts), metadata_path=str(metadata),
                       transpose=v.get("transpose", False), sample_column=v.get("sampleColumn") or "", group_column=v.get("groupColumn") or "",
                       min_count=v.get("minCount", 10), source_counts_path="" if copied_counts else str(counts),
                       source_metadata_path="" if copied_metadata else str(metadata), tags=v.get("tags", []))
        finally:
            if copied_counts:
                counts.unlink(missing_ok=True)
            if copied_metadata:
                metadata.unlink(missing_ok=True)

    def close(self):
        self.runtime.close()
        self.jobs.close()
        self.chat.close()
        self.profile.close()
