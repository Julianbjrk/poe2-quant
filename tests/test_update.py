"""The self-updater: version compare, safe swap, rollback safety. All offline
via an injected fetch and a temp install dir — no network, no real GitHub."""
import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from quant import update


def make_tarball(version, files):
    """files: {relpath: text}. Returns gzipped tar bytes like GitHub's archive,
    with a single top dir."""
    top = "poe2-quant-testbranch"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        allf = {"VERSION": version + "\n", "quant.py": "print('shim')\n",
                "quant/__init__.py": "__version__='" + version + "'\n", **files}
        for rel, text in allf.items():
            data = text.encode()
            info = tarfile.TarInfo(f"{top}/{rel}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class TestToken(unittest.TestCase):
    def setUp(self):
        # hermetic: a token in the ambient env (CI proxies inject one) must not
        # leak into these tests
        import os
        self._saved = {k: os.environ.pop(k, None) for k in ("GITHUB_TOKEN", "QUANT_GH_TOKEN")}

    def tearDown(self):
        import os
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v

    def test_env_token_wins(self):
        import os
        os.environ["QUANT_GH_TOKEN"] = "abc"
        self.assertEqual(update.token_from({"adv": {"github_token": "zzz"}}), "abc")

    def test_config_token_fallback(self):
        self.assertEqual(update.token_from({"adv": {"github_token": "fromcfg"}}), "fromcfg")

    def test_no_token_is_none(self):
        self.assertIsNone(update.token_from({"adv": {}}))


class TestVersionCompare(unittest.TestCase):
    def test_tuple_order(self):
        self.assertEqual(update._ver_tuple("1.2.0"), (1, 2, 0))
        self.assertTrue(update._ver_tuple("1.2.0") > update._ver_tuple("1.1.9"))
        self.assertTrue(update._ver_tuple("1.10.0") > update._ver_tuple("1.9.0"))

    def test_check_available(self):
        res = update.check("b", fetch=lambda url: b"9.9.9\n")
        self.assertTrue(res["available"])
        self.assertEqual(res["latest"], "9.9.9")

    def test_check_network_error_is_soft(self):
        def boom(url):
            raise OSError("offline")
        res = update.check("b", fetch=boom)
        self.assertFalse(res["available"])
        self.assertIn("err", res)


class TestApply(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dest = Path(self.tmp.name)
        (self.dest / "quant").mkdir()
        (self.dest / "quant" / "__init__.py").write_text("__version__='1.0.0'\n")
        (self.dest / "quant.py").write_text("print('old')\n")
        (self.dest / "VERSION").write_text("1.0.0\n")
        (self.dest / "config.json").write_text('{"mine":true}')   # user data
        (self.dest / "quant.db").write_text("DBDATA")             # user data

    def tearDown(self):
        self.tmp.cleanup()

    def test_swaps_code_keeps_user_data(self):
        blob = make_tarball("1.2.0", {"quant/models.py": "x=1\n"})
        res = update.apply("b", fetch=lambda url: blob, dest=self.dest, log=lambda *a: None)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["version"], "1.2.0")
        self.assertEqual((self.dest / "VERSION").read_text().strip(), "1.2.0")
        self.assertTrue((self.dest / "quant" / "models.py").exists())
        # user data untouched
        self.assertEqual((self.dest / "config.json").read_text(), '{"mine":true}')
        self.assertEqual((self.dest / "quant.db").read_text(), "DBDATA")
        # backup of the prior code exists
        self.assertTrue((self.dest / ".quant_backup" / "VERSION").exists())

    def test_broken_code_aborts_without_touching_install(self):
        blob = make_tarball("1.3.0", {"quant/bad.py": "def (oops\n"})  # syntax error
        res = update.apply("b", fetch=lambda url: blob, dest=self.dest, log=lambda *a: None)
        self.assertFalse(res["ok"])
        # install is exactly as before
        self.assertEqual((self.dest / "VERSION").read_text().strip(), "1.0.0")
        self.assertFalse((self.dest / "quant" / "bad.py").exists())

    def test_download_error_is_soft(self):
        def boom(url):
            raise OSError("no net")
        res = update.apply("b", fetch=boom, dest=self.dest, log=lambda *a: None)
        self.assertFalse(res["ok"])
        self.assertEqual((self.dest / "VERSION").read_text().strip(), "1.0.0")


if __name__ == "__main__":
    unittest.main()
