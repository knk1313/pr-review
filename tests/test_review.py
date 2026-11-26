import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import review  # noqa: E402

DIFF = """diff --git a/src/app.ts b/src/app.ts
index 1..2 100644
--- a/src/app.ts
+++ b/src/app.ts
@@ -1 +1 @@
-a
+b
diff --git a/package-lock.json b/package-lock.json
index 1..2 100644
--- a/package-lock.json
+++ b/package-lock.json
@@ -1 +1 @@
-x
+y
diff --git a/web/dist/main.js b/web/dist/main.js
--- a/web/dist/main.js
+++ b/web/dist/main.js
@@ -1 +1 @@
-x
+y
"""


class SplitDiffTest(unittest.TestCase):
    def test_splits_by_file(self):
        paths = [p for p, _ in review.split_diff(DIFF)]
        self.assertEqual(paths, ["src/app.ts", "package-lock.json", "web/dist/main.js"])


class ExcludedTest(unittest.TestCase):
    def test_lockfile_and_dist_are_excluded(self):
        self.assertTrue(review.excluded("package-lock.json"))
        self.assertTrue(review.excluded("web/dist/main.js"))
        self.assertTrue(review.excluded("assets/logo.png"))

    def test_source_is_kept(self):
        self.assertFalse(review.excluded("src/app.ts"))
        self.assertFalse(review.excluded("src/distance.ts"))


class BuildDiffTest(unittest.TestCase):
    def test_excluded_files_are_listed(self):
        diff, skipped = review.build_diff(DIFF, 100_000)
        self.assertIn("src/app.ts", diff)
        self.assertNotIn("package-lock.json", diff)
        self.assertEqual(len(skipped), 2)

    def test_over_limit_files_are_listed(self):
        big = "diff --git a/a.py b/a.py\n" + "+x\n" * 5000
        # 余りが多ければ途中まで入れ、少なければ丸ごと外す
        diff, skipped = review.build_diff(DIFF + big, 3_000)
        self.assertIn("ここから先を切った", diff)
        self.assertLessEqual(len(diff), 3_100)
        self.assertIn("a.py（途中から）", skipped)
        diff, skipped = review.build_diff(DIFF + big, 500)
        self.assertNotIn("a/a.py", diff)
        self.assertIn("a.py（差分の上限を超えたため）", skipped)


if __name__ == "__main__":
    unittest.main()
