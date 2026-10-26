from __future__ import annotations

from dataclasses import dataclass
import json
import os
import sys
import subprocess
import tempfile
import threading
from importlib import resources
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from gleskit_mcp.config import Settings
from gleskit_mcp.utils import clean_text


@dataclass(slots=True)
class CohortDownloadResult:
    counts: pd.DataFrame
    metadata: pd.DataFrame
    summary: dict[str, Any]
    meta: dict[str, Any]


class CohortDownloadBackend:
    def __init__(
        self,
        settings: Settings,
        *,
        command_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self.settings = settings
        self.command_runner = command_runner or subprocess.run
        self.rscript_bin = settings.rscript_bin
        packaged = Path(str(resources.files("gleskit_mcp").joinpath("r", "gleskit_download_cohort.R")))
        fallback = settings.project_root / "src" / "gleskit_mcp" / "r" / "gleskit_download_cohort.R"
        self.script_path = packaged if packaged.exists() else fallback

    def download(
        self,
        *,
        provider_id: str,
        dataset_id: str,
        tissue: str = "",
        sample_limit: int | None = None,
        download_mode: str = "remote",
    ) -> CohortDownloadResult:
        if not self.script_path.exists():
            raise FileNotFoundError(f"Missing cohort download script: {self.script_path}")

        with tempfile.TemporaryDirectory(prefix="gleskit_cohort_download_") as tmp:
            root = Path(tmp)
            counts_out = root / "counts.csv"
            metadata_out = root / "metadata.csv"
            summary_out = root / "summary.json"
            env = dict(os.environ)
            env.setdefault("OMP_NUM_THREADS", "1")
            env.setdefault("OPENBLAS_NUM_THREADS", "1")
            args = [
                self.rscript_bin,
                self.script_path.as_posix(),
                clean_text(provider_id),
                clean_text(dataset_id),
                clean_text(tissue),
                counts_out.as_posix(),
                metadata_out.as_posix(),
                summary_out.as_posix(),
                str(int(sample_limit)) if sample_limit is not None and int(sample_limit) > 0 else "",
            ]
            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                cwd=root,
            )
            stdout_chunks: list[str] = []
            stderr_chunks: list[str] = []

            def _drain_stderr() -> None:
                assert proc.stderr is not None
                for chunk in iter(proc.stderr.readline, ""):
                    if not chunk:
                        break
                    stderr_chunks.append(chunk)

            stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
            stderr_thread.start()

            assert proc.stdout is not None
            for line in iter(proc.stdout.readline, ""):
                if not line:
                    break
                if line.startswith("GEARS_PROGRESS:"):
                    print(line.rstrip("\n"), file=sys.stderr, flush=True)
                    continue
                stdout_chunks.append(line)

            proc.wait()
            stderr_thread.join(timeout=5)
            stderr = "".join(stderr_chunks).strip()
            if proc.returncode != 0:
                raise RuntimeError(stderr or f"Cohort download failed with exit code {proc.returncode}")
            if not counts_out.exists() or not metadata_out.exists():
                raise RuntimeError("Cohort downloader did not produce counts and metadata outputs.")
            counts = pd.read_csv(counts_out)
            metadata = pd.read_csv(metadata_out)
            summary: dict[str, Any] = {}
            meta: dict[str, Any] = {}
            if summary_out.exists():
                raw = json.loads(summary_out.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    summary = dict(raw.get("summary") or {})
                    meta = dict(raw.get("meta") or {})
            return CohortDownloadResult(counts, metadata, summary, meta)
