from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from deal_watch.__main__ import application_base_dir


class RuntimePathTests(unittest.TestCase):
    def test_pyinstaller_uses_internal_resource_directory(self):
        expected = Path("C:/DealWatch/_internal")
        with patch.object(sys, "frozen", True, create=True), patch.object(
            sys, "_MEIPASS", str(expected), create=True
        ):
            self.assertEqual(application_base_dir(), expected)


if __name__ == "__main__":
    unittest.main()
