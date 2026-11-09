#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""skillbridge —— Hermes 与 DeepSeek Harness 的技能双向互通。

两侧共用 ~/.agents/skills：DSH 原生扫描它，Hermes 通过 skills.external_dirs 指入。
本工具负责把技能写成对方能直接读取的形式，并给出可复核的体检。

快速开始：
    python -m skillbridge install      一条命令完成检测、导出、验证
    python -m skillbridge scan         只看能转多少、哪些有问题
    python -m skillbridge export       导出到 ~/.agents/skills
    python -m skillbridge import       把共享根的外来技能收进 Hermes
    python -m skillbridge verify       按 DSH 规则独立复核
    python -m skillbridge diff         两侧差异

机制细节见 README。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

try:
    import yaml
except ImportError:  # PyYAML 缺失时降级为极简解析
    yaml = None

HOME = Path.home()


def _default_hermes_home() -> Path:
    """按平台推导 Hermes 主目录；环境变量 HERMES_HOME 优先。"""
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env).expanduser()
    candidates = [
        HOME / ".hermes",
        HOME / "AppData/Local/hermes",
        HOME / "Library/Application Support/hermes",
        HOME / ".local/share/hermes",
    ]
    for c in candidates:
        if c.is_dir():
            return c
    return candidates[0]


def _default_dsh_home() -> Path:
    """DSH 主目录：环境变量 DSH_HOME 优先，默认 ~/.dsh（三平台一致）。"""
    env = os.environ.get("DSH_HOME")
    return Path(env).expanduser() if env else HOME / ".dsh"


HERMES_HOME = _default_hermes_home()
HERMES_SKILLS = HERMES_HOME / "skills"
BRIDGE_ROOT = Path(os.environ.get("SKILLBRIDGE_ROOT", HOME / ".agents/skills")).expanduser()
DSH_HOME = _default_dsh_home()
DSH_SKILLS = DSH_HOME / "skills"

SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
FM_RE = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n?", re.S)

# Hermes 有、DSH 会静默忽略的键（不致命，但要报）
HERMES_EXTRA_KEYS = {
    "license", "metadata", "version", "author", "platforms", "compatibility",
    "dependencies", "prerequisites", "allowed-tools", "tags", "category",
    "triggers", "codename", "internal", "tier", "trigger", "artifacts",
}
# 双方同名同义的键
SHARED_KEYS = {"name", "description", "disable-model-invocation", "user-invocable"}

# Hermes 有、DSH 没有对应物的工具名（跨侧后正文引用会断）
HERMES_ONLY_TOOLS = (
    "skill_view", "skill_manage", "browser_exec", "vision_analyze",
    "text_to_speech", "execute_code", "delegate_task", "web_extract",
    "browser_vault_list", "browser_vault_fill", "clarify", "cronjob_manage",
)

# DSH 实际暴露的工具名（从 @deepseek-ai/dsh-tool-* 的 lib/index.js 抽取）
DSH_TOOLS = {
    "ask_user_question", "bash", "pwsh", "edit", "read", "read_image", "write",
    "glob", "grep", "create_goal", "get_goal", "update_goal",
    "job_kill", "job_list", "job_output", "present", "ralph",
    "schedule_create", "schedule_delete", "schedule_list", "schedule_update",
    "session_event_read", "session_event_search", "session_event_trace",
    "session_search", "session_trace", "skill", "str_replace_editor",
    "list_subagent_models", "interrupt_agent", "send_message",
    "terminal_close", "terminal_list", "terminal_open", "terminal_read",
    "terminal_send", "terminal_signal", "todo_write", "web_fetch", "web_search",
    "working_directory", "load_workspace_dependencies",
}

# Hermes 工具 -> DSH 对应物；None 表示无对应，只能降级
TOOL_MAP = {
    "skill_view": "skill",
    "skill_manage": "write/str_replace_editor（直接改 SKILL.md）",
    "browser_exec": None,
    "vision_analyze": "read_image",
    "text_to_speech": None,
    "execute_code": "bash/pwsh（跑脚本）",
    "delegate_task": None,
    "web_extract": "web_fetch",
    "browser_vault_list": None,
    "browser_vault_fill": None,
    "clarify": "ask_user_question",
    "cronjob_manage": "schedule_create/schedule_update",
    "terminal": "bash/pwsh",
    "web_search": "web_search",
    "read_file": "read",
    "write_file": "write",
    "patch": "str_replace_editor",
    "search_files": "grep/glob",
}


def bridge_note(body: str) -> str:
    """为正文引用了 Hermes 专有工具的技能生成降级注记。"""
    hits = sorted({t for t in HERMES_ONLY_TOOLS if re.search(rf"\b{re.escape(t)}\b", body)})
    if not hits:
        return ""
    lines = [
        "",
        "> **[跨端注记 · 由 skillbridge 自动生成]**",
        "> 本技能原为 Hermes 编写，正文出现的下列工具在 DeepSeek Harness 中无同名实现。",
        "> 遇到时按右列替代，无对应者请改用可用工具完成同一目标，不要照抄调用。",
        ">",
    ]
    for h in hits:
        alt = TOOL_MAP.get(h)
        lines.append(f"> - `{h}` → " + (f"`{alt}`" if alt else "**无对应工具**（自行用 bash/read/write 等替代）"))
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- frontmatter

def split_frontmatter(text: str):
    """返回 (dict, body)。无 frontmatter 返回 (None, text)。"""
    m = FM_RE.match(text)
    if not m:
        return None, text
    raw = m.group(1)
    body = text[m.end():]
    if yaml is not None:
        try:
            data = yaml.safe_load(raw) or {}
            if not isinstance(data, dict):
                raise ValueError("frontmatter 不是映射")
            return data, body
        except Exception:
            pass
    # 降级：逐行 key: value
    data = {}
    for line in raw.splitlines():
        km = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if km:
            v = km.group(2).strip()
            v = _unquote(v)
            if v in ("true", "True"):
                v = True
            elif v in ("false", "False"):
                v = False
            data[km.group(1)] = v
    return data, body


def _unquote(v: str) -> str:
    """剥掉 YAML 行内引号：'x'、"x" 都还原成 x。

    降级解析器不做完整 YAML 解析，但引号是技能元数据里最常见的一种写法，
    不剥离会让 name 校验与 round-trip 全部失准。
    """
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        inner = v[1:-1]
        if v[0] == '"':
            inner = inner.replace('\\"', '"').replace("\\\\", "\\")
        else:
            inner = inner.replace("''", "'")
        return inner
    return v


def render_frontmatter(data: dict) -> str:
    if yaml is not None:
        txt = yaml.safe_dump(data, allow_unicode=True, sort_keys=False,
                             default_flow_style=False, width=10**6)
        return "---\n" + txt + "---\n"
    lines = ["---"]
    for k, v in data.items():
        if isinstance(v, bool):
            v = "true" if v else "false"
        lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 发现

def discover(root: Path):
    """按 DSH/Hermes 共同规则扫描一个技能根。

    返回 [{dir, name, path, data, body, bytes, source_root}]
    """
    out = []
    if not root.is_dir():
        return out
    for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if entry.name == ".system":
            continue
        if entry.is_dir():
            f = entry / "SKILL.md"
            if not f.is_file():
                continue
            directory = entry
        elif entry.suffix == ".md":
            f = entry
            directory = root
        else:
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        data, body = split_frontmatter(text)
        out.append({
            "dir": entry.name if entry.is_dir() else entry.stem,
            "name": (data or {}).get("name") or entry.stem,
            "path": f,
            "directory": directory,
            "data": data,
            "body": body,
            "bytes": len(text.encode("utf-8")),
            "root": str(root),
        })
    return out


def collect_hermes():
    """Hermes 全部技能（含 _archive，但标出来）。"""
    items = []
    for root, dirs, files in os.walk(HERMES_SKILLS):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        if "SKILL.md" not in files:
            continue
        p = Path(root) / "SKILL.md"
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        data, body = split_frontmatter(text)
        rel = p.relative_to(HERMES_SKILLS).as_posix()
        items.append({
            "dir": Path(root).name,
            "name": (data or {}).get("name") or Path(root).name,
            "path": p,
            "directory": Path(root),
            "data": data,
            "body": body,
            "bytes": len(text.encode("utf-8")),
            "rel": rel,
            "archived": rel.startswith("_archive/"),
            "root": str(HERMES_SKILLS),
        })
    return items


# ---------------------------------------------------------------- 兼容判定

def judge(item, target: str):
    """判定一个技能能否转到 target('dsh'|'hermes')。

    返回 dict(ok, reasons[], warnings[])
    """
    reasons, warnings = [], []
    d = item.get("data") or {}
    name = d.get("name") or item["dir"]
    desc = d.get("description")
    body = item.get("body") or ""

    if target == "dsh":
        if not isinstance(desc, str) or not desc.strip():
            reasons.append("缺 description（DSH 硬性要求）")
        if not SKILL_NAME.match(str(name)):
            reasons.append(f"name 不合 DSH 语法 {SKILL_NAME.pattern}：{name!r}")
        if "invocation" in d:
            reasons.append("含 invocation 块（DSH 不认，须改为 disable-model-invocation）")
        extra = set(d) - SHARED_KEYS
        if extra:
            warnings.append("DSH 忽略的键：" + ",".join(sorted(extra)))
        plats = str(d.get("platforms") or "")
        if plats and "windows" not in plats.lower():
            warnings.append(f"平台声明不含 windows：{plats}")
        # 正文工具依赖：DSH 没有这些工具，技能内容在但调用会断
        hits = sorted(t for t in HERMES_ONLY_TOOLS if re.search(rf"\b{re.escape(t)}\b", body))
        if hits:
            warnings.append("正文引用 Hermes 专有工具：" + ",".join(hits))
    elif target == "hermes":
        if not isinstance(desc, str) or not desc.strip():
            reasons.append("缺 description")
        if yaml is None:
            warnings.append("PyYAML 未安装，frontmatter 用降级解析")
    return {"ok": not reasons, "reasons": reasons, "warnings": warnings}


# ---------------------------------------------------------------- 转换

def to_dsh(item):
    """Hermes 技能 -> DSH 可加载形态。

    返回 (frontmatter_dict, body, changed_keys[])
    """
    d = dict(item["data"] or {})
    changed = []
    out = {"name": d.get("name") or item["dir"], "description": d.get("description", "")}

    # invocation 块 -> 扁平布尔键
    inv = d.get("invocation")
    if isinstance(inv, dict):
        if inv.get("modelInvocable") is False:
            out["disable-model-invocation"] = True
        if inv.get("userInvocable") is False:
            out["user-invocable"] = False
        changed.append("invocation -> disable-model-invocation/user-invocable")
    elif "disable-model-invocation" in d:
        out["disable-model-invocation"] = bool(d["disable-model-invocation"])

    if "whenToUse" in d or "when-to-use" in d:
        w = d.get("whenToUse") or d.get("when-to-use")
        out["whenToUse"] = w
        changed.append("whenToUse 保留（DSH 识别）")

    # 其余键（license/version/author/platforms 等）DSH 一律忽略，不写出。
    # 保留它们只会让 DSH 侧多出一堆无意义的元数据。
    return out, item["body"], changed


def to_hermes(item):
    """DSH 技能 -> Hermes 可加载形态。"""
    d = dict(item["data"] or {})
    out = {"name": d.get("name") or item["dir"], "description": d.get("description", "")}
    if "disable-model-invocation" in d:
        out["disable-model-invocation"] = d["disable-model-invocation"]
    if "user-invocable" in d:
        out["user-invocable"] = d["user-invocable"]
    if "whenToUse" in d:
        out["whenToUse"] = d["whenToUse"]
    return out, item["body"]


# ---------------------------------------------------------------- 写入

def safe_target(bridge_root: Path, name: str) -> Path:
    """防目录穿越：目标必须在 bridge_root 之下。"""
    t = (bridge_root / name).resolve()
    r = bridge_root.resolve()
    if not str(t).startswith(str(r) + os.sep) and t != r:
        raise ValueError(f"目标越界：{t}")
    return t


def copy_tree(src_dir: Path, dst_dir: Path, force: bool):
    """复制技能目录；已存在且非同源时需 force。"""
    if dst_dir.exists():
        if not force:
            raise FileExistsError(str(dst_dir))
        shutil.rmtree(dst_dir)
    dst_dir.mkdir(parents=True, exist_ok=True)
    for item in src_dir.iterdir():
        if item.name in ("__pycache__", ".git"):
            continue
        tgt = dst_dir / item.name
        if item.is_dir():
            shutil.copytree(item, tgt, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__", ".git"))
        elif item.is_file():
            shutil.copy2(item, tgt)


def manifest_path():
    return BRIDGE_ROOT.parent / ".skillbridge-manifest.json"


def load_manifest():
    p = manifest_path()
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"version": 1, "entries": {}}


def save_manifest(m):
    p = manifest_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")


def file_hash(path: Path):
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


# ---------------------------------------------------------------- 命令

def cmd_scan(args):
    herm = collect_hermes()
    dsh = discover(DSH_SKILLS)
    bridge = discover(BRIDGE_ROOT)
    rows = []
    for it in herm:
        j = judge(it, "dsh")
        rows.append({
            "side": "hermes",
            "name": it["name"],
            "rel": it["rel"],
            "archived": it["archived"],
            "bytes": it["bytes"],
            "ok": j["ok"],
            "reasons": j["reasons"],
            "warnings": j["warnings"],
        })
    if args.json:
        print(json.dumps({
            "hermes_total": len(herm),
            "hermes_archived": sum(1 for i in herm if i["archived"]),
            "bridge": [b["name"] for b in bridge],
            "dsh_home": [d["name"] for d in dsh],
            "rows": rows,
        }, ensure_ascii=False, indent=1))
        return 0

    ok = [r for r in rows if r["ok"]]
    bad = [r for r in rows if not r["ok"]]
    print(f"Hermes 技能总数      : {len(herm)}  (其中 _archive {sum(1 for i in herm if i['archived'])})")
    print(f"可直接转 DSH         : {len(ok)}")
    print(f"需处理才可转         : {len(bad)}")
    print(f"已入共享根 ~/.agents : {len(bridge)}  (明细: skillbridge verify)")
    print(f"DSH 私有根 ~/.dsh    : {len(dsh)}")
    if bad:
        print("\n需处理名单：")
        for r in bad:
            print(f"  {r['name']:42s} {'; '.join(r['reasons'])}")
    warn = [r for r in rows if r["ok"] and r["warnings"]]
    if warn:
        tool_broken = [r for r in warn if any("专有工具" in w for w in r["warnings"])]
        meta_only = [r for r in warn if r not in tool_broken]
        print(f"\n告警分两类：")
        print(f"  [A] 仅元数据被忽略（技能可用，没有影响）: {len(meta_only)}")
        print(f"  [B] 正文引用 Hermes 专有工具（转过去执行会断）: {len(tool_broken)}")
        if tool_broken:
            print("  B 类名单（前 20）:")
            for r in tool_broken[:20]:
                hit = [w for w in r["warnings"] if "专有工具" in w][0]
                print(f"    {r['name']:40s} {hit.split('：')[1][:60]}")
            if len(tool_broken) > 20:
                print(f"    …… 其余 {len(tool_broken)-20} 条见 --json")
    return 0


def cmd_export(args):
    """Hermes -> 共享根 ~/.agents/skills（DSH 与 Hermes 双方都读）。

    --target dsh 时改写 ~/.dsh/skills（仅 DSH 可见）。
    """
    herm = collect_hermes()
    if not args.include_archive:
        herm = [h for h in herm if not h["archived"]]
    if args.only:
        herm = [h for h in herm if h["name"] in args.only or h["dir"] in args.only]
    dest_root = DSH_SKILLS if args.target == "dsh" else BRIDGE_ROOT
    man = load_manifest()
    done, skip, fail = [], [], []
    dest_root.mkdir(parents=True, exist_ok=True)

    for it in herm:
        j = judge(it, "dsh")
        if not j["ok"]:
            fail.append((it["name"], j["reasons"]))
            continue
        tgt_name = it["name"]
        tgt = safe_target(dest_root, tgt_name)
        try:
            if args.dry_run:
                done.append((tgt_name, "dry-run"))
                continue
            copy_tree(it["directory"], tgt, force=args.force)
            fm, body, changed = to_dsh(it)
            note = bridge_note(body)
            if note:
                # 注记插在正文首个标题之前，不打断原有结构
                m = re.search(r"^#\s", body, re.M)
                if m:
                    body = body[:m.start()] + note.lstrip("\n") + "\n" + body[m.start():]
                else:
                    body = note.lstrip("\n") + "\n" + body
                changed.append("插入跨端工具注记")
            (tgt / "SKILL.md").write_text(
                render_frontmatter(fm) + body, encoding="utf-8"
            )
            man["entries"][tgt_name] = {
                "from": "hermes",
                "src": str(it["path"]),
                "dst": str(tgt / "SKILL.md"),
                "hash": file_hash(tgt / "SKILL.md"),
                "at": datetime.now().isoformat(timespec="seconds"),
            }
            done.append((tgt_name, ",".join(changed) or "原样"))
        except FileExistsError:
            skip.append((tgt_name, "已存在（用 --force 覆盖）"))
        except Exception as e:
            fail.append((tgt_name, [str(e)]))

    if not args.dry_run:
        save_manifest(man)
    print(f"导出到 {dest_root}")
    print(f"完成: {len(done)}  跳过: {len(skip)}  失败: {len(fail)}"
          + ("  [dry-run]" if args.dry_run else ""))
    for n, c in done[:10]:
        print(f"  + {n:42s} {c}")
    if len(done) > 10:
        print(f"  …… 其余 {len(done)-10} 条")
    for n, c in skip:
        print(f"  = {n:42s} {c}")
    for n, r in fail:
        print(f"  ! {n:42s} {'; '.join(r)}")
    return 0


def cmd_import(args):
    """~/.agents/skills -> Hermes skills/（让外来技能进 Hermes 目录）。"""
    src = discover(BRIDGE_ROOT)
    if args.only:
        src = [s for s in src if s["name"] in args.only]
    done, skip, fail = [], [], []
    for it in src:
        j = judge(it, "hermes")
        if not j["ok"]:
            fail.append((it["name"], j["reasons"]))
            continue
        tgt = HERMES_SKILLS / it["name"]
        try:
            if args.dry_run:
                done.append(it["name"])
                continue
            copy_tree(it["directory"], tgt, force=args.force)
            fm, body = to_hermes(it)
            fm.setdefault("platforms", ["windows", "linux", "macos"])
            (tgt / "SKILL.md").write_text(render_frontmatter(fm) + body, encoding="utf-8")
            done.append(it["name"])
        except FileExistsError:
            skip.append(it["name"])
        except Exception as e:
            fail.append((it["name"], [str(e)]))
    print(f"导入完成: {len(done)}  跳过: {len(skip)}  失败: {len(fail)}"
          + ("  [dry-run]" if args.dry_run else ""))
    for n in done:
        print(f"  + {n}")
    for n in skip:
        print(f"  = {n} 已存在（用 --force 覆盖）")
    for n, r in fail:
        print(f"  ! {n} {'; '.join(r)}")
    return 0


def cmd_verify(args):
    """独立复核：不信任 manifest，重新按两侧规则解析共享根。"""
    bridge = discover(BRIDGE_ROOT)
    print(f"共享根: {BRIDGE_ROOT}")
    healthy = broken = 0
    for b in bridge:
        j = judge(b, "dsh")
        mark = "OK  " if j["ok"] else "FAIL"
        if j["ok"]:
            healthy += 1
        else:
            broken += 1
        extra = ""
        if j["warnings"]:
            extra = "  (告警: " + "; ".join(j["warnings"])[:80] + ")"
        print(f"  [{mark}] {b['name']:42s} {b['bytes']:>8,d}B{extra}")
        if not j["ok"]:
            for r in j["reasons"]:
                print(f"         - {r}")
    print(f"\n健康 {healthy} / 异常 {broken} / 共 {len(bridge)}")
    # Hermes 侧能否看见
    herm = collect_hermes()
    arch = sum(1 for h in herm if h["archived"])
    outside = {h["name"] for h in herm if not h["archived"]} - {b["name"] for b in bridge}
    print(f"Hermes 技能总数 {len(herm)}（其中 _archive {arch}）")
    print(f"其中尚未导出到共享根的 {len(outside)} 个")
    return 0 if broken == 0 else 1


def cmd_diff(args):
    """列出 Hermes 与共享根之间的差异（区分正常转换差异与真实内容漂移）。"""
    herm = {h["name"]: h for h in collect_hermes() if not h["archived"]}
    bridge = {b["name"]: b for b in discover(BRIDGE_ROOT)}
    only_h = sorted(set(herm) - set(bridge))
    only_b = sorted(set(bridge) - set(herm))
    both = sorted(set(herm) & set(bridge))
    print(f"仅 Hermes 有（未导出）: {len(only_h)}")
    print(f"仅共享根有（外来/未导入）: {len(only_b)}  -> {', '.join(only_b) or '无'}")
    print(f"两侧都有: {len(both)}")
    body_same, fm_only, real_drift = 0, [], []
    for n in both:
        a = herm[n]
        b = bridge[n]
        if file_hash(a["path"]) == file_hash(b["path"]):
            body_same += 1
            continue
        # 剥离 frontmatter 后比较正文
        ba = (a.get("body") or "").strip()
        bb = (b.get("body") or "").strip()
        # 共享根可能被插入了跨端注记，比对时剔除
        bb_clean = re.sub(
            r"^> \*\*\[跨端注记.*?(?=\n#|\n\n[^>])", "", bb, flags=re.S).strip()
        if ba == bb_clean or ba in bb:
            fm_only.append(n)
        else:
            real_drift.append(n)
    print(f"完全相同: {body_same}")
    print(f"仅 frontmatter 被裁（正文一致，正常）: {len(fm_only)}")
    print(f"正文/注记有差异: {len(real_drift)}")
    if real_drift:
        print("  差异名单（前 12）:", ", ".join(real_drift[:12]))
        print("  深入核对: diff <hermes路径> <共享根路径>")
    return 0


def cmd_install(args):
    """一条命令完成落地：检测 -> 导出 -> 验证。给不需要理解细节的使用者。"""
    print("=" * 58)
    print("skillbridge install")
    print("=" * 58)

    ok = True

    # 1) 环境检测
    print("\n[1/3] 环境检测")
    print(f"  Hermes 技能目录 : {HERMES_SKILLS}  {'存在' if HERMES_SKILLS.is_dir() else '缺失'}")
    print(f"  共享根          : {BRIDGE_ROOT}  {'存在' if BRIDGE_ROOT.is_dir() else '将创建'}")
    print(f"  DSH 目录        : {DSH_HOME}  {'存在' if DSH_HOME.is_dir() else '未安装或未运行'}")

    if not HERMES_SKILLS.is_dir():
        print("\n  未找到 Hermes 技能目录。")
        print("  请设置环境变量 HERMES_HOME 指向 Hermes 主目录（其下应有 skills/ 子目录），然后重跑。")
        return 2

    if not DSH_HOME.is_dir() and args.target == "dsh":
        print(f"\n  目标 {DSH_SKILLS} 的上级目录不存在，无法写入。")
        print("  请先安装并启动一次 DeepSeek Harness，或改用默认 --target bridge。")
        return 2

    # 2) 导出
    print(f"\n[2/3] 导出 Hermes -> {'~/.dsh/skills' if args.target == 'dsh' else '~/.agents/skills'}")
    exp = argparse.Namespace(
        only=None, force=args.force, dry_run=False,
        include_archive=args.include_archive, target=args.target,
    )
    rc = cmd_export(exp)
    if rc not in (0, None):
        ok = False

    # 3) 验证
    print("\n[3/3] 验证")
    if args.target == "dsh":
        n = 0
        if DSH_SKILLS.is_dir():
            for entry in sorted(DSH_SKILLS.iterdir()):
                f = entry / "SKILL.md" if entry.is_dir() else entry
                if f.is_file():
                    n += 1
        print(f"  ~/.dsh/skills 下共 {n} 个技能文件")
        print("  DSH 会以 rank 400 加载该目录（优先于 ~/.agents/skills 的 500）。")
    else:
        rc = cmd_verify(argparse.Namespace())
        if rc not in (0, None):
            ok = False

    print("\n" + "=" * 58)
    if ok:
        print("完成。重启 DSH 后，技能即出现在其技能列表中。")
        print("Hermes 侧无需重启：它在下次会话时扫描 ~/.agents/skills。")
    else:
        print("部分步骤有问题，请查看上面的输出。")
    print("=" * 58)
    return 0 if ok else 1


def main(argv=None):
    p = argparse.ArgumentParser(prog="skillbridge", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version="skillbridge 1.0.0")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="扫描并报告可转换性")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_scan)

    e = sub.add_parser("export", help="Hermes -> 共享根（DSH 可读）")
    e.add_argument("--only", nargs="*")
    e.add_argument("--force", action="store_true")
    e.add_argument("--dry-run", action="store_true")
    e.add_argument("--include-archive", action="store_true")
    e.add_argument("--target", choices=["bridge", "dsh"], default="bridge",
                   help="bridge=~/.agents/skills（双向可见，默认）；dsh=~/.dsh/skills（仅 DSH）")
    e.set_defaults(func=cmd_export)

    i = sub.add_parser("import", help="共享根 -> Hermes")
    i.add_argument("--only", nargs="*")
    i.add_argument("--force", action="store_true")
    i.add_argument("--dry-run", action="store_true")
    i.set_defaults(func=cmd_import)

    v = sub.add_parser("verify", help="独立复核共享根健康度")
    v.set_defaults(func=cmd_verify)

    d = sub.add_parser("diff", help="两侧差异")
    d.set_defaults(func=cmd_diff)

    n = sub.add_parser("install", help="一条命令完成落地（检测+导出+验证）")
    n.add_argument("--force", action="store_true")
    n.add_argument("--include-archive", action="store_true")
    n.add_argument("--target", choices=["bridge", "dsh"], default="bridge")
    n.set_defaults(func=cmd_install)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
