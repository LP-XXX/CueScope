from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from deal_watch.security import CredentialStore, protect, unprotect


@unittest.skipUnless(sys.platform == "win32", "DPAPI 仅在 Windows 验证")
class DpapiTests(unittest.TestCase):
    def test_roundtrip_and_credential_file_is_not_plaintext(self):
        secret = "unit-test-only-not-a-real-credential"
        self.assertEqual(unprotect(protect(secret.encode("utf-8"))), secret.encode("utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            store = CredentialStore(Path(directory))
            store.save("http://127.0.0.1:18080/webhook", secret)
            self.assertNotIn(secret.encode(), store.path.read_bytes())
            self.assertEqual(store.load(), ("http://127.0.0.1:18080/webhook", secret))

    def test_rejected_url_does_not_replace_existing_encrypted_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CredentialStore(Path(directory))
            store.save("http://127.0.0.1:18080/webhook", "test-only")
            before = store.path.read_bytes()
            with self.assertRaises(ValueError):
                store.save("http://example.invalid/private-token", "test-only")
            self.assertEqual(store.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
