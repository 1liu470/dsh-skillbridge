"""skillbridge 核心逻辑测试。

不依赖真实 Hermes/DSH 安装：全部用临时目录构造样本。
运行：python -m pytest tests/ -q   或   python tests/test_convert.py
"""

import sys
import tempfile
import unittest
from pathlib import Path

# 允许从仓库根直接跑
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skillbridge import cli  # noqa: E402


SAMPLE = """---
name: demo-skill
description: 一个用于测试的技能
license: MIT
author: someone
version: "1.0.0"
---

# Demo

正文第一段。
"""

SAMPLE_QUOTED = """---
name: "quoted-name"
description: "带引号的描述"
---

# Quoted
"""

SAMPLE_INVOCATION = """---
name: inv-skill
description: 有 invocation 块的技能
invocation:
  modelInvocable: false
  userInvocable: false
---

# Inv
"""

SAMPLE_TOOL_REF = """---
name: tool-ref
description: 引用 Hermes 专有工具
---

# Tool

用 `browser_exec` 打开页面，再用 `vision_analyze` 看一眼。
"""


def write_skill(root: Path, dirname: str, text: str) -> Path:
    d = root / dirname
    d.mkdir(parents=True, exist_ok=True)
    f = d / "SKILL.md"
    f.write_text(text, encoding="utf-8")
    return f


class TestFrontmatter(unittest.TestCase):
    def test_split_basic(self):
        data, body = cli.split_frontmatter(SAMPLE)
        self.assertEqual(data["name"], "demo-skill")
        self.assertEqual(data["description"], "一个用于测试的技能")
        self.assertIn("# Demo", body)

    def test_quoted_values_are_unquoted(self):
        """YAML 引号必须剥离，否则 name 校验会误判。"""
        data, _ = cli.split_frontmatter(SAMPLE_QUOTED)
        self.assertEqual(data["name"], "quoted-name")
        self.assertEqual(data["description"], "带引号的描述")

    def test_version_quoted_stays_string(self):
        data, _ = cli.split_frontmatter(SAMPLE)
        self.assertNotIn('"', str(data.get("version")))

    def test_no_frontmatter(self):
        data, body = cli.split_frontmatter("# just body\n")
        self.assertIsNone(data)
        self.assertIn("just body", body)


class TestJudgement(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _item(self, text, dirname="x"):
        p = write_skill(self.tmp, dirname, text)
        items = cli.discover(self.tmp / dirname)
        self.assertEqual(len(items), 1, "样本应被识别为一个技能")
        item = items[0]
        item["body"] = cli.split_frontmatter(text)[1]
        item["data"] = cli.split_frontmatter(text)[0]
        return item

    def test_valid_skill_passes(self):
        j = cli.judge(self._item(SAMPLE), "dsh")
        self.assertTrue(j["ok"], j["reasons"])

    def test_quoted_name_passes(self):
        """引号包裹的合法名称不应被判失败。"""
        j = cli.judge(self._item(SAMPLE_QUOTED), "dsh")
        self.assertTrue(j["ok"], j["reasons"])

    def test_missing_description_fails(self):
        text = "---\nname: no-desc\n---\n# X\n"
        j = cli.judge(self._item(text), "dsh")
        self.assertFalse(j["ok"])
        self.assertTrue(any("description" in r for r in j["reasons"]))

    def test_bad_name_fails(self):
        text = "---\nname: Bad_Name\ndescription: x\n---\n# X\n"
        j = cli.judge(self._item(text), "dsh")
        self.assertFalse(j["ok"])

    def test_invocation_block_detected(self):
        j = cli.judge(self._item(SAMPLE_INVOCATION), "dsh")
        self.assertFalse(j["ok"])
        self.assertTrue(any("invocation" in r for r in j["reasons"]))


class TestConversion(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _item(self, text):
        p = write_skill(self.tmp, "s", text)
        item = cli.discover(self.tmp / "s")[0]
        data, body = cli.split_frontmatter(text)
        item["data"] = data
        item["body"] = body
        return item

    def test_extra_keys_dropped(self):
        fm, body, changed = cli.to_dsh(self._item(SAMPLE))
        self.assertEqual(set(fm) - {"name", "description"}, set())
        self.assertIn("name", fm)
        self.assertIn("description", fm)

    def test_invocation_becomes_flat_key(self):
        fm, _, _ = cli.to_dsh(self._item(SAMPLE_INVOCATION))
        self.assertTrue(fm.get("disable-model-invocation"))
        self.assertFalse(fm.get("user-invocable", True))

    def test_body_preserved(self):
        _, body, _ = cli.to_dsh(self._item(SAMPLE))
        self.assertIn("正文第一段", body)

    def test_tool_note_inserted(self):
        """引用 Hermes 专有工具时，注记应插入且提到该工具。"""
        item = self._item(SAMPLE_TOOL_REF)
        note = cli.bridge_note(item["body"])
        self.assertIn("browser_exec", note)
        self.assertIn("vision_analyze", note)

    def test_note_before_first_heading(self):
        item = self._item(SAMPLE_TOOL_REF)
        note = cli.bridge_note(item["body"])
        head = note.strip().splitlines()[0]
        self.assertTrue(head.startswith("#") or head.startswith(">"),
                        f"注记位置异常，首行：{head!r}")


class TestRoundTrip(unittest.TestCase):
    def test_hermes_to_dsh_keeps_body_bytes(self):
        tmp = Path(tempfile.mkdtemp())
        write_skill(tmp, "rt", SAMPLE_TOOL_REF)
        item = cli.discover(tmp)[0]
        data, body = cli.split_frontmatter(SAMPLE_TOOL_REF)
        item["data"], item["body"] = data, body
        fm, out_body, _ = cli.to_dsh(item)
        # 正文主体必须原样存在
        self.assertIn("用 `browser_exec` 打开页面", out_body)
        # 重新渲染后能被再次解析
        text = cli.render_frontmatter(fm) + out_body
        data2, _ = cli.split_frontmatter(text)
        self.assertEqual(data2["name"], "tool-ref")


if __name__ == "__main__":
    unittest.main(verbosity=2)
