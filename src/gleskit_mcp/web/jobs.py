from __future__ import annotations

import json
import subprocess
import sys
import threading
import uuid

from gleskit_mcp.models import utc_now_iso
from .common import WebError, identifier
from .lifecycle import stop_child


class Jobs:
    """Download work runs in owned CLI process groups, not the HTTP worker."""

    def __init__(self, profile):
        self.profile = profile
        self.lock = threading.RLock()
        self.rows, self.processes = {}, {}
        self.closing = False

    def start(self, *, title, providerId, datasetId, tissue="", sampleLimit=0, tags=None):
        identifier(providerId)
        if not isinstance(datasetId, str) or not datasetId or len(datasetId) > 300:
            raise WebError("Choose a valid dataset.")
        if type(sampleLimit) is not int or sampleLimit < 0:
            raise WebError("Sample limit must be a nonnegative integer.")
        with self.lock:
            if self.closing:
                raise WebError("The UI is shutting down; no new download was started.", 409)
            if any(row["can_cancel"] for row in self.rows.values()):
                raise WebError("A cohort download is already active. Finish or cancel it first.", 409)
            key, now = "job_" + uuid.uuid4().hex, utc_now_iso()
            row = {"job_id": key, "title": title, "provider_id": providerId, "dataset_id": datasetId,
                   "tissue": tissue, "sample_limit": sampleLimit, "status": "queued", "stage": "Queued",
                   "event_status": "queued", "progress_percent": None, "started_at": now, "updated_at": now,
                   "finished_at": None, "can_cancel": True, "log": [], "result": None, "error": None}
            self.rows[key] = row
        args = [sys.executable, "-m", "gleskit_mcp", "--project-root", str(self.profile.project),
                "--workspace-root", str(self.profile.workspace), "download-cohort", "--title", title,
                "--provider-id", providerId, "--dataset-id", datasetId, "--tissue", tissue,
                "--sample-limit", str(sampleLimit)]
        if tags:
            args += ["--tags", *tags]
        threading.Thread(target=self._run, args=(key, args), daemon=True).start()
        return row.copy()

    def _run(self, key, args):
        output = None
        try:
            child = subprocess.Popen(args, cwd=self.profile.home, env=self.profile.environment(),
                                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     start_new_session=True)
            with self.lock:
                self.processes[key] = child
                cancelled = self.closing or self.rows[key]["status"] == "cancelled"
                if not cancelled:
                    self.rows[key].update(status="running", stage="Downloading and validating cohort")
            if cancelled:
                stop_child(child)
            output, _ = child.communicate()
            with self.lock:
                if self.rows[key]["status"] != "cancelled":
                    if child.returncode:
                        self.rows[key].update(status="failed", stage="Failed", error="Cohort download failed. Check provider availability and R dependencies in Settings.")
                    else:
                        self.rows[key].update(status="completed", stage="Complete", progress_percent=100,
                                              result=json.loads(output))
        except (OSError, ValueError):
            with self.lock:
                self.rows[key].update(status="failed", stage="Failed", error="Cannot complete the cohort download.")
        finally:
            with self.lock:
                self.rows[key].update(can_cancel=False, finished_at=utc_now_iso(), updated_at=utc_now_iso())
                self.processes.pop(key, None)

    def get(self, key):
        identifier(key)
        with self.lock:
            if key not in self.rows:
                raise WebError("Unknown download job.", 404)
            return self.rows[key].copy()

    def cancel(self, key):
        self.get(key)
        with self.lock:
            if not self.rows[key]["can_cancel"]:
                return self.rows[key].copy()
            self.rows[key].update(status="cancelled", stage="Cancelled", can_cancel=False)
            child = self.processes.get(key)
        stop_child(child)
        return self.get(key)

    def close(self):
        with self.lock:
            self.closing = True
        for key in list(self.rows):
            if self.get(key)["can_cancel"]:
                self.cancel(key)
