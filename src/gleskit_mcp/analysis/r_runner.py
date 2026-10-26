from __future__ import annotations

from importlib import resources
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
from threading import Lock
import time
from typing import Any, Callable

from gleskit_mcp.config import Settings
from gleskit_mcp.providers.base import ProviderError
from gleskit_mcp.utils import clean_text


class RJsonScriptRunner:
    _jobs_lock = Lock()
    _jobs: dict[str, dict[str, Any]] = {}

    def __init__(
        self,
        settings: Settings,
        *,
        command_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        rscript_bin: str | None = None,
    ) -> None:
        self.settings = settings
        self.command_runner = command_runner or subprocess.run
        self.rscript_bin = clean_text(rscript_bin or settings.rscript_bin or "Rscript") or "Rscript"

    def run(self, script_name: str, payload: dict[str, Any], *, capability: str, timeout_seconds: int = 180) -> dict[str, Any]:
        job_id = self._register_job(script_name=script_name, capability=capability, timeout_seconds=timeout_seconds)
        packaged_script = resources.files("gleskit_mcp").joinpath("r", script_name)
        fallback_script = self.settings.project_root / "src" / "gleskit_mcp" / "r" / script_name

        with resources.as_file(packaged_script) as packaged_path:
            script_path = packaged_path if packaged_path.exists() else fallback_script
            if not script_path.exists():
                self._finish_job(job_id, status="failed", error=f"Script is missing: {script_path}")
                raise ProviderError(f"GLESKit local {capability} script is missing: {script_path}")

            with tempfile.TemporaryDirectory(prefix="gleskit_rjson_") as tmp:
                tmpdir = Path(tmp)
                input_path = tmpdir / "input.json"
                output_path = tmpdir / "output.json"
                input_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

                try:
                    command = [
                        self.rscript_bin,
                        str(script_path),
                        "--input",
                        str(input_path),
                        "--output",
                        str(output_path),
                    ]
                    if self.command_runner is subprocess.run:
                        completed = self._run_default_command(
                            command,
                            timeout_seconds=timeout_seconds,
                            env=self._runtime_env(),
                            on_started=lambda pid: self._mark_job_started(job_id, pid),
                        )
                    else:
                        completed = self.command_runner(
                            command,
                            capture_output=True,
                            text=True,
                            timeout=timeout_seconds,
                            check=False,
                            env=self._runtime_env(),
                        )
                except FileNotFoundError as exc:
                    self._finish_job(job_id, status="failed", error=str(exc))
                    raise ProviderError(
                        f"GLESKit local {capability} requires '{self.rscript_bin}' to be installed and available on PATH."
                    ) from exc
                except subprocess.TimeoutExpired as exc:
                    self._finish_job(job_id, status="timed_out", error=f"Timed out after {timeout_seconds}s")
                    raise ProviderError(f"GLESKit local {capability} timed out while running the R backend.") from exc

                if completed.returncode != 0:
                    stderr = clean_text(completed.stderr or "")
                    stdout = clean_text(completed.stdout or "")
                    detail = stderr or stdout or f"R exited with status {completed.returncode}"
                    self._finish_job(job_id, status="failed", error=detail)
                    raise ProviderError(f"GLESKit local {capability} failed: {detail}")

                if not output_path.exists():
                    self._finish_job(job_id, status="failed", error="R did not produce an output payload.")
                    raise ProviderError(f"GLESKit local {capability} did not produce an output payload.")

                try:
                    raw = json.loads(output_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError as exc:
                    self._finish_job(job_id, status="failed", error="R produced invalid JSON output.")
                    raise ProviderError(f"GLESKit local {capability} produced invalid JSON output.") from exc

        if not isinstance(raw, dict):
            self._finish_job(job_id, status="failed", error="R returned a non-object response.")
            raise ProviderError(f"GLESKit local {capability} returned a non-object response.")
        self._finish_job(job_id, status="succeeded")
        return raw

    @classmethod
    def _register_job(cls, *, script_name: str, capability: str, timeout_seconds: int) -> str:
        job_id = f"rjob_{int(time.time() * 1000)}_{os.urandom(4).hex()}"
        with cls._jobs_lock:
            cls._jobs[job_id] = {
                "job_id": job_id,
                "script_name": script_name,
                "capability": capability,
                "timeout_seconds": int(timeout_seconds),
                "status": "queued",
                "pid": None,
                "started_at": None,
                "finished_at": None,
                "error": None,
            }
        return job_id

    @classmethod
    def _mark_job_started(cls, job_id: str, pid: int) -> None:
        with cls._jobs_lock:
            job = cls._jobs.get(job_id)
            if job is not None:
                job.update({"status": "running", "pid": int(pid), "started_at": time.time()})

    @classmethod
    def _finish_job(cls, job_id: str, *, status: str, error: str | None = None) -> None:
        with cls._jobs_lock:
            job = cls._jobs.get(job_id)
            if job is not None:
                job.update({"status": status, "finished_at": time.time(), "error": clean_text(error) or None})

    @classmethod
    def runtime_status(cls) -> dict[str, Any]:
        with cls._jobs_lock:
            jobs = [dict(job) for job in cls._jobs.values()]
        jobs.sort(key=lambda job: (job.get("started_at") or 0, job.get("job_id") or ""), reverse=True)
        active = [job for job in jobs if job.get("status") in {"queued", "running", "cancelling"}]
        return {
            "active_job_count": len(active),
            "active_jobs": active,
            "recent_jobs": jobs[:50],
            "healthy": not active,
        }

    @classmethod
    def cancel_job(cls, job_id: str) -> dict[str, Any]:
        with cls._jobs_lock:
            job = cls._jobs.get(clean_text(job_id))
            if job is None:
                raise ProviderError(f"Unknown R job id: {clean_text(job_id)}")
            pid = job.get("pid")
            status = job.get("status")
            if status not in {"queued", "running", "cancelling"}:
                return dict(job)
            job["status"] = "cancelling"
        if pid:
            try:
                os.killpg(int(pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
        cls._finish_job(clean_text(job_id), status="cancelled", error="Cancellation requested.")
        with cls._jobs_lock:
            return dict(cls._jobs[clean_text(job_id)])

    @staticmethod
    def _run_default_command(
        command: list[str],
        *,
        timeout_seconds: int,
        env: dict[str, str],
        on_started: Callable[[int], None] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run R in an isolated process group so timeout cleanup includes workers.

        ``subprocess.run(..., timeout=...)`` terminates the direct Rscript
        process but not child workers spawned by Bioconductor/fgsea.  Those
        workers can survive a timed-out MCP call and starve later requests.
        """
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            start_new_session=True,
        )
        if on_started is not None:
            on_started(process.pid)
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
            raise subprocess.TimeoutExpired(command, timeout_seconds, output=exc.output, stderr=exc.stderr) from exc
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)

    def _runtime_env(self) -> dict[str, str]:
        r_lib_path = self.settings.r_lib_root
        r_lib_path.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        r_threads = str(max(1, int(getattr(self.settings, "r_threads", 1) or 1)))
        env["LC_ALL"] = "C"
        env["LANG"] = "C"
        env["R_LIBS_USER"] = str(r_lib_path)
        env["OMP_NUM_THREADS"] = r_threads
        env["OMP_THREAD_LIMIT"] = r_threads
        env["OPENBLAS_NUM_THREADS"] = r_threads
        env["MKL_NUM_THREADS"] = r_threads
        env["VECLIB_MAXIMUM_THREADS"] = r_threads
        env["MC_CORES"] = r_threads
        env["R_PARALLEL_NUM_THREADS"] = r_threads
        r_max_vsize = str(getattr(self.settings, "r_max_vsize", "") or "").strip()
        if r_max_vsize:
            env["R_MAX_VSIZE"] = r_max_vsize
        return env
