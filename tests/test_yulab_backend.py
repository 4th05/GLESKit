from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from gleskit_mcp.analysis.yulab import YuLabAnalysisBackend
from gleskit_mcp.config import load_settings


class YuLabAnalysisBackendTests(unittest.TestCase):
    def test_pathway_enrichment_honors_enclosing_evaluation_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"GLESKIT_EVAL_TOOL_TIMEOUT_SECONDS": "1200"},
        ):
            captured: dict[str, object] = {}

            def fake_runner(cmd, *, capture_output, text, timeout, check, env):
                captured["timeout"] = timeout
                output_path = Path(cmd[cmd.index("--output") + 1])
                output_path.write_text(
                    json.dumps({"provider": "yulab_clusterprofiler", "summary": "Stubbed enrichment.", "terms": []}),
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

            backend = YuLabAnalysisBackend(load_settings(Path(tmp)), command_runner=fake_runner)
            result = backend.pathway_enrichment(genes=["TP53", "CDK2"], sources=["GO:BP"])

            self.assertEqual(result.provider, "yulab_clusterprofiler")
            self.assertEqual(captured["timeout"], 1200)


if __name__ == "__main__":
    unittest.main()
