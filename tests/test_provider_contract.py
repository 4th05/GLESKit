from __future__ import annotations

import unittest

from gleskit_mcp.providers.base import ProviderError


class ProviderErrorContractTests(unittest.TestCase):
    def test_structured_error_preserves_retry_contract(self) -> None:
        error = ProviderError(
            "The requested metadata label was not found.",
            error_code="invalid_input",
            retryable=False,
            suggested_next_step="Use the exact label returned by inspection.",
            diagnostic="available_columns=sample,condition",
        )

        self.assertEqual(
            error.structured(),
            {
                "error_code": "invalid_input",
                "retryable": False,
                "suggested_next_step": "Use the exact label returned by inspection.",
                "diagnostic": "available_columns=sample,condition",
            },
        )
        self.assertIn("error_code=invalid_input", str(error))


if __name__ == "__main__":
    unittest.main()
