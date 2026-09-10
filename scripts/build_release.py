# -*- coding: utf-8 -*-
"""WP-06 任务 D：两类 release 打包 + SHA256 + 递归 manifest。

1. portable release：白名单打包 v3.1 源码（loopx/、lease.py、runners/、faults/、
   adapter/reasonix/、tests/、scripts/、README.md）→ ecc-v3.1-portable.zip
2. Reasonix-integrated release：portable 内容 + 技能根目录布局
   （skills/ecc-v3.1-production/ 含 SKILL.md 与 scripts，即 adapter/reasonix 内容）
   → ecc-v3.1-reasonix-integrated.zip
3. 每个 zip 生成递归 manifest（相对路径/字节/SHA-256）→ manifest-<name>.txt；
   全部产物 SHA 汇总 → sha256.txt

硬性检查（任一命中即拒绝出包）：
- 0 __pycache__ / *.pyc
- 0 运行数据（STATE.json / RUN-LOG.jsonl / *.jsonl / checkpoint / lease.json）
- 0 密钥（api_key/apikey 赋值、sk- 长样本、password 赋值）
- 0 硬编码绝对路径（盘符路径字面量）

用法：
    python scripts/build_release.py [--v31-root <DIR>] [--delivery-dir <DIR>]
"""
from __future__ import annotations

import argparse
import hashlib
import io
import os
import re
import sys
import zipfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

WHITELIST = (
    "loopx",
    "lease.py",
    "runners",
    "faults",
    "adapter/reasonix",
    "tests",
    "scripts",
    "README.md",
)

RUNTIME_DATA_PATTERNS = (
    re.compile(r"(^|/)STATE\.json$"),
    re.compile(r"(^|/)RUN-LOG\.jsonl$"),
    re.compile(r"(^|/)LOOP-CONTRACT\.json$"),
    re.compile(r"(^|/)lease\.json$"),
    re.compile(r"\.jsonl$"),
    re.compile(r"checkpoint", re.IGNORECASE),
)

SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"(?i)\bapi_?key\b\s*[:=]\s*['\"]?[^'\s\"']{6,}"),
    re.compile(r"(?i)\bpassword\b\s*[:=]\s*['\"]?[^'\s\"']{6,}"),
)

ABSPATH_PATTERN = re.compile(r"(?<![A-Za-z0-9_.-])[A-Za-z]:[\\/]")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_text(name: str) -> bool:
    return name.endswith((".py", ".md", ".ps1", ".json", ".txt", ".yml",
                           ".yaml", ".toml"))


def scan_text(relpath: str, text: str, errors: list) -> None:
    if "__pycache__" in relpath.replace("\\", "/") or relpath.endswith(".pyc"):
        errors.append(f"{relpath}: 命中禁止项 __pycache__/*.pyc")
        return
    if RUNTIME_DATA_PATTERNS and any(
            p.search(relpath.replace("\\", "/")) for p in RUNTIME_DATA_PATTERNS):
        errors.append(f"{relpath}: 命中禁止项 运行数据")
    for pat in SECRET_PATTERNS:
        m = pat.search(text)
        if m:
            snippet = m.group(0)[:40]
            errors.append(f"{relpath}: 疑似密钥 {snippet!r}")
    m = ABSPATH_PATTERN.search(text)
    if m:
        line = text.count("\n", 0, m.start()) + 1
        errors.append(f"{relpath}:{line}: 硬编码绝对路径 {m.group(0)!r}")


def collect_files(root: str) -> dict:
    """按白名单收集 {arcname: filepath}。"""
    files: dict = {}
    for entry in WHITELIST:
        path = os.path.join(root, entry)
        if os.path.isfile(path):
            files[entry] = path
            continue
        if not os.path.isdir(path):
            continue
        for dirpath, dirnames, filenames in os.walk(path):
            dirnames[:] = [d for d in dirnames
                           if d != "__pycache__"]
            for fn in sorted(filenames):
                if fn.endswith(".pyc"):
                    continue
                full = os.path.join(dirpath, fn)
                arc = os.path.relpath(full, root).replace("\\", "/")
                files[arc] = full
    return dict(sorted(files.items()))


def build_zip(files: dict, zip_path: str, extra_files: dict) -> list:
    """构造 zip（固定时间戳保证可复现），返回 [(arcname, size, sha256)]。"""
    manifest_entries: list = []
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for arcname, src in files.items():
            data = open(src, "rb").read()
            info = zipfile.ZipInfo(arcname, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
            manifest_entries.append((arcname, len(data), sha256_bytes(data)))
        for arcname, data in extra_files.items():
            info = zipfile.ZipInfo(arcname, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
            manifest_entries.append(
                (arcname, len(data), sha256_bytes(data)))
    return manifest_entries


def verify_zip(zip_path: str, manifest_entries: list, errors: list) -> None:
    """重新打开 zip 逐项核对 manifest；zip 内文件再过禁止项扫描。"""
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
        for name in names:
            if "__pycache__" in name or name.endswith(".pyc"):
                errors.append(f"zip 内禁止项: {name}")
        expect = {a: (s, h) for a, s, h in manifest_entries}
        for name in names:
            data = zf.read(name)
            if name not in expect:
                errors.append(f"zip 内未登记于 manifest 的文件: {name}")
                continue
            size, digest = expect[name]
            if len(data) != size:
                errors.append(f"zip 内 {name} 字节数 {len(data)} != manifest {size}")
            if sha256_bytes(data) != digest:
                errors.append(f"zip 内 {name} SHA 与 manifest 不一致")
            if is_text(name):
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                scan_text(name, text, errors)


def write_manifest(entries: list, zip_path: str, manifest_path: str) -> None:
    lines = ["# ecc-v3.1 release manifest（相对路径\t字节数\tsha256）"]
    for arcname, size, digest in entries:
        lines.append(f"{arcname}\t{size}\t{digest}")
    zip_sha = sha256_file(zip_path)
    zip_size = os.path.getsize(zip_path)
    lines.append(f"# archive\t{os.path.basename(zip_path)}\t{zip_size}\t{zip_sha}")
    with open(manifest_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")


def default_delivery_dir() -> str:
    worktree = os.path.dirname(_ROOT)          # v3.1 的父 = worktree
    ws = os.path.dirname(worktree)             # workspaces
    return os.path.join(ws, "ecc-v3.1-production-delivery")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="WP-06 打包")
    parser.add_argument("--v31-root", default=_ROOT)
    parser.add_argument("--delivery-dir", default=None)
    args = parser.parse_args(argv)

    root = os.path.abspath(args.v31_root)
    delivery = os.path.abspath(args.delivery_dir or default_delivery_dir())
    os.makedirs(delivery, exist_ok=True)

    errors: list = []

    # ---- 1. 收集 + 源文件禁止项扫描 ----
    files = collect_files(root)
    for arcname, src in files.items():
        if is_text(arcname):
            with open(src, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
            scan_text(arcname, text, errors)
    if not files:
        errors.append("白名单未收集到任何文件")

    # ---- 2. Reasonix 技能根布局（adapter/reasonix 内容） ----
    adapter_root = os.path.join(root, "adapter", "reasonix")
    skill_files: dict = {}
    if os.path.isdir(adapter_root):
        for dirpath, dirnames, filenames in os.walk(adapter_root):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for fn in sorted(filenames):
                if fn.endswith(".pyc"):
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, adapter_root).replace("\\", "/")
                with open(full, "rb") as f:
                    data = f.read()
                skill_files[f"skills/ecc-v3.1-production/{rel}"] = data
    else:
        errors.append("缺少 adapter/reasonix 目录")

    # ---- 3. 出包（先写临时名，验证通过再定名） ----
    results: dict = {}
    if not errors:
        tmp_portable = os.path.join(delivery, ".tmp-ecc-v3.1-portable.zip")
        tmp_integrated = os.path.join(delivery,
                                      ".tmp-ecc-v3.1-reasonix-integrated.zip")
        try:
            p_entries = build_zip(files, tmp_portable, {})
            verify_zip(tmp_portable, p_entries, errors)
            i_entries = build_zip(files, tmp_integrated, skill_files)
            verify_zip(tmp_integrated, i_entries, errors)
        except OSError as exc:
            errors.append(f"打包失败: {exc}")
        if not errors:
            os.replace(tmp_portable,
                       os.path.join(delivery, "ecc-v3.1-portable.zip"))
            os.replace(tmp_integrated,
                       os.path.join(delivery,
                                    "ecc-v3.1-reasonix-integrated.zip"))
            results["portable"] = (p_entries, os.path.join(
                delivery, "ecc-v3.1-portable.zip"))
            results["integrated"] = (i_entries, os.path.join(
                delivery, "ecc-v3.1-reasonix-integrated.zip"))
    for tmp in (".tmp-ecc-v3.1-portable.zip",
                ".tmp-ecc-v3.1-reasonix-integrated.zip"):
        p = os.path.join(delivery, tmp)
        if os.path.exists(p):
            os.unlink(p)

    if errors:
        print("打包被拒（硬性检查未通过）：")
        for e in errors:
            print(" -", e)
        return 1

    # ---- 4. manifest + sha256 汇总 ----
    manifest_paths = []
    manifest_names = {"portable": "portable",
                      "integrated": "reasonix-integrated"}
    for key in ("portable", "integrated"):
        entries, zip_path = results[key]
        mpath = os.path.join(delivery, f"manifest-{manifest_names[key]}.txt")
        write_manifest(entries, zip_path, mpath)
        manifest_paths.append(mpath)

    sha_lines = ["# ecc-v3.1 delivery SHA256 汇总"]
    all_artifacts = [results[n][1] for n in ("portable", "integrated")]
    all_artifacts.extend(manifest_paths)
    for path in all_artifacts:
        sha_lines.append(f"{sha256_file(path)}  {os.path.basename(path)}")
    with open(os.path.join(delivery, "sha256.txt"), "w", encoding="utf-8",
              newline="\n") as f:
        f.write("\n".join(sha_lines) + "\n")

    for name in ("portable", "integrated"):
        entries, zip_path = results[name]
        print(f"{name}: {os.path.basename(zip_path)} "
              f"size={os.path.getsize(zip_path)} entries={len(entries)}")
    print(f"manifest + sha256 已写入 {delivery}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
