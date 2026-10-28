import unittest

from core import engine
from core import runtime_diagnostics as diagnostics


class DiagnosticMergeTests(unittest.TestCase):
    def test_planning_prompt_fits_redaction_budget(self):
        self.assertIsInstance(diagnostics._scrub_text(engine._AGENT_SYSTEM), str)

    def test_oversized_json_still_fails_closed(self):
        raw = '{' + ','.join(f'"field{i}":0' for i in range(diagnostics.MAX_ITEMS + 1)) + '}'
        with self.assertRaises(ValueError):
            diagnostics._scrub_text(raw)

    def test_large_text_redacts_sensitive_fields(self):
        raw = 'examples: ' + ' '.join(f'{{"field{i}":0}}' for i in range(150))
        raw += ' {"cred\\u0065ntial":"SYNTHETIC_482019"}'
        self.assertNotIn('SYNTHETIC_482019', diagnostics._scrub_text(raw))


if __name__ == '__main__':
    unittest.main()
