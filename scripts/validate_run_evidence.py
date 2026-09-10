# -*- coding: utf-8 -*-
"""WP-06 严格证据验证器（V3.1 版）。

校验内容：
1. 20 件合同文件存在性 + 关键字段（对象/数组结构、必填字段、状态合法值）；
2. manifest 与 zip 内文件 SHA 一致性（相对路径/字节/SHA-256 逐项核对，
   sha256.txt 与实文件一致）；
3. 四态合法：layer-status 状态、final_status 四态、completion-ledger 状态、
   声明验收矩阵 disposition；
4. 禁止项：release 内 0 __pycache__/*.pyc、0 运行数据、0 密钥、0 硬编码绝对路径；
5. 自带正反例自测：合规样本必须 pass=True，违规样本必须 pass=False 且
   错误命中预期关键词。

用法：
    python scripts/validate_run_evidence.py <evidence_dir> [--delivery-dir DIR]
    python scripts/validate_run_evidence.py --selftest
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

JSON_FILES = (
    "run-config.json",
    "layer-status.json",
    "declared-requirements.json",
    "completion-ledger.json",
    "source-confidence-register.json",
    "tool-inventory.json",
    "agents.json",
    "work-packages.json",
    "execution-claims.json",
    "artifact-manifest.json",
    "deterministic-checks.json",
    "claim-acceptance-matrix.json",
    "rounds.json",
    "checkpoints.json",
    "delivery-safety-check.json",
    "handoff.json",
    "evidence-index.json",
    "final-acceptance.json",
)
TEXT_FILES = ("final-acceptance.md", "final-report.md")

LAYER_STATUSES = {"pending", "in_progress", "complete", "blocked"}
FINAL_STATUSES = {"PASS", "PARTIAL", "BLOCKED", "REJECTED"}
LEDGER_STATUSES = {"ACCEPTED", "PENDING_AUDIT", "NOT_STARTED", "BLOCKED",
                   "REJECTED"}
MATRIX_DISPOSITIONS = {"ACCEPTED", "REJECTED", "PENDING"}

DELIVERY_ZIPS = ("ecc-v3.1-portable.zip", "ecc-v3.1-reasonix-integrated.zip")
DELIVERY_MANIFESTS = ("manifest-portable.txt",
                      "manifest-reasonix-integrated.txt")

SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"(?i)\bapi_?key\b\s*[:=]\s*['\"]?[^'\s\"']{6,}"),
    re.compile(r"(?i)\bpassword\b\s*[:=]\s*['\"]?[^'\s\"']{6,}"),
)
ABSPATH_PATTERN = re.compile(r"(?<![A-Za-z0-9_.-])[A-Za-z]:[\\/]")
RUNTIME_DATA_PATTERNS = (
    re.compile(r"(^|/)STATE\.json$"),
    re.compile(r"(^|/)RUN-LOG\.jsonl$"),
    re.compile(r"(^|/)LOOP-CONTRACT\.json$"),
    re.compile(r"(^|/)lease\.json$"),
    re.compile(r"\.jsonl$"),
    re.compile(r"checkpoint", re.IGNORECASE),
)


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path, errors: list) -> object:
    if not path.is_file():
        errors.append(f"缺少必需文件：{path.name}")
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"文件无法解析：{path.name}；原因：{exc}")
        return None


def read_text(path: Path, errors: list) -> str:
    if not path.is_file():
        errors.append(f"缺少必需文件：{path.name}")
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        errors.append(f"文件无法读取：{path.name}；原因：{exc}")
        return ""


def nonempty(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict, tuple, set)):
        return len(value) > 0
    return True


def as_list(value, key: str):
    if isinstance(value, list):
        return value
    if isinstance(value, dict) and isinstance(value.get(key), list):
        return value[key]
    return None


# ---------------------------------------------------------------- 关键字段
def validate_json_fields(data: dict, errors: list) -> None:
    def need_list(obj, key, objname, minimum=1):
        lst = as_list(obj, key) if isinstance(obj, dict) else None
        if not isinstance(lst, list) or len(lst) < minimum:
            errors.append(f"{objname}.{key} 必须是非空数组（至少 {minimum} 项）")
            return []
        return lst

    # run-config
    cfg = data.get("run-config.json")
    if isinstance(cfg, dict):
        if not nonempty(cfg.get("task_id")):
            errors.append("run-config.json 缺 task_id")
        need_list(cfg, "work_packages", "run-config.json")
    # layer-status：12 层完整
    ls = data.get("layer-status.json")
    if isinstance(ls, dict):
        layers = as_list(ls, "layers")
        if isinstance(layers, list):
            nums = [item.get("layer") for item in layers
                    if isinstance(item, dict)]
            if len(layers) != 12 or sorted(nums) != list(range(1, 13)):
                errors.append("layer-status.json 必须恰好覆盖 12 层（layer 1-12）")
            for item in layers:
                if not isinstance(item, dict):
                    continue
                if item.get("status") not in LAYER_STATUSES:
                    errors.append(f"layer-status.json 第 {item.get('layer')} 层 "
                                  f"非法状态 {item.get('status')!r}")
                if not nonempty(item.get("name")):
                    errors.append("layer-status.json 层缺 name")
    # declared-requirements：20 条标准
    dr = data.get("declared-requirements.json")
    if isinstance(dr, dict):
        sc = as_list(dr, "success_criteria")
        if isinstance(sc, list):
            if len(sc) != 20:
                errors.append(f"declared-requirements.json success_criteria "
                              f"须 20 条，实际 {len(sc)}")
            for item in sc:
                if isinstance(item, dict) and not nonempty(item.get("text")):
                    errors.append("declared-requirements.json 标准缺 text")
    # completion-ledger
    ledger = data.get("completion-ledger.json")
    ledger_list = as_list(ledger, "entries") if isinstance(ledger, dict) \
        else (ledger if isinstance(ledger, list) else None)
    if isinstance(ledger_list, list):
        if not ledger_list:
            errors.append("completion-ledger.json 为空")
        for item in ledger_list:
            if not isinstance(item, dict):
                continue
            if not nonempty(item.get("requirement_id")):
                errors.append("completion-ledger.json 条目缺 requirement_id")
            if item.get("status") not in LEDGER_STATUSES:
                errors.append(f"completion-ledger.json 条目 "
                              f"{item.get('requirement_id')!r} 非法状态 "
                              f"{item.get('status')!r}")
    # source-confidence-register
    scr = data.get("source-confidence-register.json")
    if isinstance(scr, dict):
        need_list(scr, "entries", "source-confidence-register.json")
    # tool-inventory
    ti = data.get("tool-inventory.json")
    if isinstance(ti, dict):
        need_list(ti, "tools", "tool-inventory.json")
    # agents
    ag = data.get("agents.json")
    if isinstance(ag, dict):
        need_list(ag, "agents", "agents.json")
    # work-packages：WP-01..WP-06
    wp = data.get("work-packages.json")
    if isinstance(wp, dict):
        pkgs = need_list(wp, "packages", "work-packages.json")
        ids = {p.get("id") for p in pkgs if isinstance(p, dict)}
        missing = {f"WP-0{i}" for i in range(1, 7)} - ids
        if missing:
            errors.append(f"work-packages.json 缺工作包：{sorted(missing)}")
    # execution-claims
    ec = data.get("execution-claims.json")
    claims = as_list(ec, "claims") if isinstance(ec, dict) \
        else (ec if isinstance(ec, list) else None)
    if isinstance(claims, list):
        if not claims:
            errors.append("execution-claims.json 为空")
        for item in claims:
            if not isinstance(item, dict):
                continue
            for field, chinese in (("claim_id", "claim_id"),
                                   ("work_package_id", "work_package_id"),
                                   ("statement_chinese", "statement_chinese")):
                if not nonempty(item.get(field)):
                    errors.append(f"execution-claims.json "
                                  f"{item.get('claim_id', '?')} 缺 {chinese}")
    # artifact-manifest（SHA 一致性）
    am = data.get("artifact-manifest.json")
    artifacts = as_list(am, "artifacts") if isinstance(am, dict) \
        else (am if isinstance(am, list) else None)
    if isinstance(artifacts, list):
        if not artifacts:
            errors.append("artifact-manifest.json 为空")
        for item in artifacts:
            if not isinstance(item, dict):
                continue
            aid = item.get("artifact_id", "?")
            if not nonempty(aid):
                errors.append("artifact-manifest.json 条目缺 artifact_id")
            sha = item.get("sha256")
            if not (isinstance(sha, str) and len(sha) == 64):
                errors.append(f"产物 {aid} 缺合法 sha256")
                continue
            path = item.get("path")
            if not nonempty(path):
                errors.append(f"产物 {aid} 缺 path")
                continue
            if os.path.isfile(path) and sha256_file(path).lower() != sha.lower():
                errors.append(f"产物 {aid} SHA 与文件不一致")
    # deterministic-checks
    dc = data.get("deterministic-checks.json")
    checks = as_list(dc, "checks") if isinstance(dc, dict) \
        else (dc if isinstance(dc, list) else None)
    if isinstance(checks, list):
        if not checks:
            errors.append("deterministic-checks.json 为空")
        for item in checks:
            if not isinstance(item, dict):
                continue
            if not nonempty(item.get("check_id")):
                errors.append("deterministic-checks.json 条目缺 check_id")
            if item.get("result") not in ("PASS", "FAIL", "SKIPPED"):
                errors.append(f"检查 {item.get('check_id', '?')} 非法 result "
                              f"{item.get('result')!r}")
    # claim-acceptance-matrix
    cam = data.get("claim-acceptance-matrix.json")
    matrix = as_list(cam, "matrix") if isinstance(cam, dict) \
        else (cam if isinstance(cam, list) else None)
    if isinstance(matrix, list):
        if not matrix:
            errors.append("claim-acceptance-matrix.json 为空")
        for item in matrix:
            if not isinstance(item, dict):
                continue
            if item.get("disposition") not in MATRIX_DISPOSITIONS:
                errors.append(f"矩阵条目 {item.get('claim_id', '?')} 非法 "
                              f"disposition {item.get('disposition')!r}")
    # rounds
    rd = data.get("rounds.json")
    if isinstance(rd, dict):
        need_list(rd, "rounds", "rounds.json")
    # checkpoints
    cp = data.get("checkpoints.json")
    if isinstance(cp, dict):
        cps = need_list(cp, "checkpoints", "checkpoints.json")
        for item in cps:
            if isinstance(item, dict) and not nonempty(item.get("id")):
                errors.append("checkpoints.json 条目缺 id")
    # delivery-safety-check
    dsc = data.get("delivery-safety-check.json")
    if isinstance(dsc, dict):
        if not isinstance(dsc.get("accepted"), bool):
            errors.append("delivery-safety-check.json 缺布尔字段 accepted")
        need_list(dsc, "checks", "delivery-safety-check.json")
    # handoff
    ho = data.get("handoff.json")
    if isinstance(ho, dict) and nonempty(ho.get("summary")):
        pass
    elif isinstance(ho, dict):
        errors.append("handoff.json 缺 summary")
    # evidence-index
    ei = data.get("evidence-index.json")
    if isinstance(ei, dict):
        items = as_list(ei, "evidence") or as_list(ei, "items")
        if not items:
            errors.append("evidence-index.json 缺 evidence/items 数组")
    # final-acceptance
    fa = data.get("final-acceptance.json")
    if isinstance(fa, dict):
        if fa.get("final_status") not in FINAL_STATUSES:
            errors.append(f"final-acceptance.json final_status 非法："
                          f"{fa.get('final_status')!r}（须 PASS/PARTIAL/"
                          f"BLOCKED/REJECTED）")
        for field, chinese in (("status_code", "status_code"),
                               ("final_signer_agent_id", "final_signer_agent_id")):
            if not nonempty(fa.get(field)):
                errors.append(f"final-acceptance.json 缺 {chinese}")


def validate_texts(texts: dict, errors: list) -> None:
    for name, content in texts.items():
        if len(content.strip()) < 120:
            errors.append(f"{name} 内容过短（<120 字符）")
        if not re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", content):
            errors.append(f"{name} 必须以中文为主体")


# ---------------------------------------------------------------- delivery
def parse_manifest(path: Path) -> tuple:
    """返回 (entries, archive_sha)。entries: {arcname: (size, sha256)}。"""
    entries: dict = {}
    archive_sha = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        parts = raw.split("\t")
        if len(parts) == 4 and parts[0] == "archive":
            archive_sha = parts[3]
            continue
        if len(parts) == 3:
            entries[parts[0]] = (int(parts[1]), parts[2])
    return entries, archive_sha


def validate_delivery(delivery: Path, errors: list) -> None:
    for name in DELIVERY_ZIPS:
        if not (delivery / name).is_file():
            errors.append(f"delivery 缺 {name}")
    for name in DELIVERY_MANIFESTS:
        if not (delivery / name).is_file():
            errors.append(f"delivery 缺 {name}")
    sha_txt = delivery / "sha256.txt"
    if not sha_txt.is_file():
        errors.append("delivery 缺 sha256.txt")
        return

    # sha256.txt 与实文件一致
    sha_map: dict = {}
    for raw in sha_txt.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        parts = raw.split()
        if len(parts) == 2 and len(parts[0]) == 64:
            sha_map[parts[1]] = parts[0]
    for name in DELIVERY_ZIPS + DELIVERY_MANIFESTS:
        path = delivery / name
        if path.is_file():
            declared = sha_map.get(name)
            actual = sha256_file(str(path))
            if declared is not None and declared.lower() != actual:
                errors.append(f"sha256.txt 中 {name} 与实文件 SHA 不一致")
            elif declared is None:
                errors.append(f"sha256.txt 缺 {name} 条目")

    # manifest 与 zip 逐项核对
    zip_by_manifest = {
        "manifest-portable.txt": "ecc-v3.1-portable.zip",
        "manifest-reasonix-integrated.txt": "ecc-v3.1-reasonix-integrated.zip",
    }
    for mname, zname in zip_by_manifest.items():
        mpath = delivery / mname
        zpath = delivery / zname
        if not mpath.is_file() or not zpath.is_file():
            continue
        entries, archive_sha = parse_manifest(mpath)
        if archive_sha is not None:
            if archive_sha.lower() != sha256_file(str(zpath)):
                errors.append(f"{mname} 声明的 zip SHA 与实际不符")
        try:
            zf = zipfile.ZipFile(str(zpath))
            names = zf.namelist()
            expect = set(entries)
            for name in names:
                data = zf.read(name)
                if name not in expect:
                    errors.append(f"{zname} 内 {name} 未登记于 manifest")
                    continue
                size, digest = entries[name]
                if len(data) != size:
                    errors.append(f"{zname} 内 {name} 字节数 {len(data)} "
                                  f"!= manifest {size}")
                if hashlib.sha256(data).hexdigest() != digest.lower():
                    errors.append(f"{zname} 内 {name} SHA 与 manifest 不一致")
                if (name.endswith((".py", ".md", ".ps1", ".json", ".txt"))
                        or True):
                    try:
                        text = data.decode("utf-8")
                    except UnicodeDecodeError:
                        text = ""
                    scan_zip_member(name, text, zname, errors)
            for name in expect - set(names):
                errors.append(f"{zname} 缺 manifest 登记的文件 {name}")
            zf.close()
        except zipfile.BadZipFile as exc:
            errors.append(f"{zname} 无法打开：{exc}")


def scan_zip_member(name: str, text: str, zname: str, errors: list) -> None:
    normalized = name.replace("\\", "/")
    if "__pycache__" in normalized or normalized.endswith(".pyc"):
        errors.append(f"{zname} 禁止项 __pycache__/*.pyc：{name}")
    if any(p.search(normalized) for p in RUNTIME_DATA_PATTERNS):
        errors.append(f"{zname} 禁止项 运行数据：{name}")
    for pat in SECRET_PATTERNS:
        m = pat.search(text)
        if m:
            errors.append(f"{zname} 疑似密钥：{name} 片段 {m.group(0)[:40]!r}")
    m = ABSPATH_PATTERN.search(text)
    if m:
        line = text.count("\n", 0, m.start()) + 1
        errors.append(f"{zname} 硬编码绝对路径：{name}:{line} {m.group(0)!r}")


# ---------------------------------------------------------------- 主入口
def validate(evidence_dir: Path, delivery_dir: Path = None) -> dict:
    errors: list = []
    warnings: list = []
    data = {name: read_json(evidence_dir / name, errors)
            for name in JSON_FILES}
    texts = {name: read_text(evidence_dir / name, errors)
             for name in TEXT_FILES}
    if errors:
        return result(errors, warnings)
    validate_json_fields(data, errors)
    validate_texts(texts, errors)
    if delivery_dir is not None:
        validate_delivery(delivery_dir, errors)
    else:
        warnings.append("未提供 --delivery-dir，跳过 manifest/zip SHA 一致性校验")
    return result(errors, warnings)


def result(errors: list, warnings: list) -> dict:
    return {
        "pass": not errors,
        "中文状态": "严格证据验证通过" if not errors else "严格证据验证失败",
        "错误": errors,
        "警告": warnings,
        "说明": "本结果只检查可确定结构；最终验收由独立 audit 签署。",
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="严格证据验证器（V3.1）")
    parser.add_argument("evidence_dir", nargs="?")
    parser.add_argument("--delivery-dir", default=None)
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)
    if args.selftest:
        return selftest()
    if not args.evidence_dir:
        print("用法：validate_run_evidence.py <evidence_dir> "
              "[--delivery-dir DIR]", file=sys.stderr)
        return 2
    delivery = Path(args.delivery_dir).resolve() if args.delivery_dir else None
    output = validate(Path(args.evidence_dir).resolve(), delivery)
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if output["pass"] else 1


# ---------------------------------------------------------------- 自测
def selftest() -> int:
    base = Path(tempfile.mkdtemp(prefix="ecc-v31-validator-selftest-"))
    failures: list = []
    try:
        ok_evidence = make_compliant_evidence(base)
        ok_delivery = make_delivery(base)
        out_ok = validate(ok_evidence, ok_delivery)
        if not out_ok["pass"]:
            failures.append("合规样本应 pass=True：" + "; ".join(out_ok["错误"][:3]))

        # 违规 1：缺文件
        d1 = base / "viol-missing"
        shutil.copytree(ok_evidence, d1)
        (d1 / "rounds.json").unlink()
        out = validate(d1, ok_delivery)
        if out["pass"] or not any("缺少必需文件：rounds.json" in e
                                  for e in out["错误"]):
            failures.append("违规样本(缺文件) 未按预期失败")

        # 违规 2：zip 内 pycache
        d2 = base / "viol-pycache"
        make_violation_zip(d2, "ecc-v3.1-portable.zip", b"x = 1\n",
                           "__pycache__/junk.pyc")
        out = validate(ok_evidence, d2)
        if out["pass"] or not any("pycache" in e for e in out["错误"]):
            failures.append("违规样本(pycache) 未按预期失败")

        # 违规 3：zip 内密钥
        d3 = base / "viol-secret"
        make_violation_zip(d3, "ecc-v3.1-portable.zip",
                           ("secret_key = " + "\"sk-" + "A" * 24 + "\"\n")
                           .encode("utf-8"), "leak.py")
        out = validate(ok_evidence, d3)
        if out["pass"] or not any("密钥" in e for e in out["错误"]):
            failures.append("违规样本(密钥) 未按预期失败")

        # 违规 4：zip 内绝对路径
        d4 = base / "viol-abspath"
        make_violation_zip(d4, "ecc-v3.1-portable.zip",
                           ("path = " + "\"C:" + "\\" + "\\evil\\\"\n")
                           .encode("utf-8"), "abs.py")
        out = validate(ok_evidence, d4)
        if out["pass"] or not any("绝对路径" in e for e in out["错误"]):
            failures.append("违规样本(绝对路径) 未按预期失败")

        # 违规 5：manifest SHA 篡改
        d5 = base / "viol-sha"
        shutil.copytree(ok_delivery, d5)
        manifest = d5 / "manifest-portable.txt"
        lines = manifest.read_text(encoding="utf-8").splitlines()
        lines[-1] = lines[-1].replace(lines[-1][-32:], "0" * 32)
        manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
        out = validate(ok_evidence, d5)
        if out["pass"] or not any("不一致" in e or "不符" in e
                                  for e in out["错误"]):
            failures.append("违规样本(manifest SHA 篡改) 未按预期失败")

        # 违规 6：final_status 非法
        d6 = base / "viol-status"
        shutil.copytree(ok_evidence, d6)
        fa = json.loads((d6 / "final-acceptance.json").read_text("utf-8"))
        fa["final_status"] = "MAYBE"
        (d6 / "final-acceptance.json").write_text(
            json.dumps(fa, ensure_ascii=False, indent=2), encoding="utf-8")
        out = validate(d6, ok_delivery)
        if out["pass"] or not any("final_status" in e for e in out["错误"]):
            failures.append("违规样本(final_status 非法) 未按预期失败")
    finally:
        shutil.rmtree(base, ignore_errors=True)

    print("SELFTEST:", "PASS" if not failures else "FAIL")
    for f in failures:
        print(" -", f)
    return 0 if not failures else 1


def make_compliant_evidence(base: Path) -> Path:
    d = base / "evidence-ok"
    d.mkdir(parents=True, exist_ok=True)
    layer = {"schema": "ecc-v3.1-layer-status", "task_id": "SELFTEST",
             "layers": [
                 {"layer": i, "name": f"Layer {i}",
                  "status": "complete", "evidence_ref": "x"}
                 for i in range(1, 13)]}
    write_json(d / "layer-status.json", layer)
    write_json(d / "run-config.json",
               {"schema": "x", "task_id": "SELFTEST",
                "work_packages": ["WP-01"]})
    write_json(d / "declared-requirements.json",
               {"schema": "x", "task_id": "SELFTEST",
                "success_criteria": [
                    {"id": i + 1, "text": f"标准 {i + 1}"} for i in range(20)]})
    write_json(d / "completion-ledger.json",
               {"schema": "x", "task_id": "SELFTEST", "entries": [
                   {"requirement_id": f"R{i:02d}", "status": "PENDING_AUDIT",
                    "evidence_ids": ["E-1"]} for i in range(1, 21)]})
    write_json(d / "source-confidence-register.json",
               {"schema": "x", "entries": [{"item": "a"}]})
    write_json(d / "tool-inventory.json",
               {"schema": "x", "tools": [{"name": "python"}]})
    write_json(d / "agents.json",
               {"schema": "x", "agents": [{"id": "a1", "role": "execution"}]})
    write_json(d / "work-packages.json",
               {"schema": "x", "packages": [
                   {"id": f"WP-0{i}", "name": f"包{i}"} for i in range(1, 7)]})
    write_json(d / "execution-claims.json",
               {"schema": "x", "claims": [
                   {"claim_id": "CLAIM-001", "work_package_id": "WP-01",
                    "statement_chinese": "完成说明",
                    "artifact_ids": ["ART-001"]}]})
    a_txt = base / "a.txt"
    a_txt.write_text("样本产物内容", encoding="utf-8")
    write_json(d / "artifact-manifest.json",
               {"schema": "x", "artifacts": [
                   {"artifact_id": "ART-001", "work_package_id": "WP-01",
                    "path": str(a_txt),
                    "sha256": sha256_file(str(a_txt)),
                    "kind": "样本"}]})
    write_json(d / "deterministic-checks.json",
               {"schema": "x", "checks": [
                   {"check_id": "CHECK-001", "result": "PASS",
                    "exit_code": 0}]})
    write_json(d / "claim-acceptance-matrix.json",
               {"schema": "x", "matrix": [
                   {"claim_id": "CLAIM-001", "disposition": "PENDING"}]})
    write_json(d / "rounds.json",
               {"schema": "x", "rounds": [{"round": 1, "action": "实现"}]})
    write_json(d / "checkpoints.json",
               {"schema": "x", "checkpoints": [
                   {"id": "cp-01", "at": "2026-08-14T00:00:00Z",
                    "action": "检查点"}]})
    write_json(d / "delivery-safety-check.json",
               {"schema": "x", "accepted": True,
                "checks": [{"item": "禁止项", "ok": True}]})
    write_json(d / "handoff.json",
               {"schema": "x", "summary": "交接摘要（中文）"})
    write_json(d / "evidence-index.json",
               {"schema": "x", "evidence": [{"evidence_id": "E-1"}]})
    write_json(d / "final-acceptance.json",
               {"schema": "x", "final_status": "PASS", "status_code": "OK",
                "final_signer_agent_id": "audit-1",
                "status_code_evidence_ids": ["E-1"]})
    (d / "final-acceptance.md").write_text(
        "中文最终状态：通过。" + "证据齐全，验收通过。" * 20,
        encoding="utf-8")
    (d / "final-report.md").write_text(
        "最终报告：原目标已全部完成。" + "证据与结论以中文为主体。" * 20,
        encoding="utf-8")
    return d


def make_delivery(base: Path) -> Path:
    d = base / "delivery-ok"
    d.mkdir(parents=True, exist_ok=True)
    content = {"ok.py": b"print('ok')\n", "README.md": "# 说明\n".encode()}
    for zname in DELIVERY_ZIPS:
        with zipfile.ZipFile(str(d / zname), "w", zipfile.ZIP_DEFLATED) as zf:
            for arc, data in content.items():
                info = zipfile.ZipInfo(arc, date_time=(1980, 1, 1, 0, 0, 0))
                zf.writestr(info, data)
    for mname, zname in zip(DELIVERY_MANIFESTS, DELIVERY_ZIPS):
        lines = [f"# {mname}"]
        for arc, data in content.items():
            lines.append(f"{arc}\t{len(data)}\t"
                         f"{hashlib.sha256(data).hexdigest()}")
        lines.append(f"# archive\t{zname}\t"
                     f"{(d / zname).stat().st_size}\t"
                     f"{sha256_file(str(d / zname))}")
        (d / mname).write_text("\n".join(lines) + "\n", encoding="utf-8")
    sha_lines = ["# sha"]
    for name in DELIVERY_ZIPS + DELIVERY_MANIFESTS:
        sha_lines.append(f"{sha256_file(str(d / name))}  {name}")
    (d / "sha256.txt").write_text("\n".join(sha_lines) + "\n",
                                  encoding="utf-8")
    return d


def make_violation_zip(d: Path, zname: str, data: bytes, arc: str) -> Path:
    """构造完整违规 delivery 目录：目标 zip 含违规内容，其余交付件合规。"""
    d.mkdir(parents=True, exist_ok=True)
    manifest_names = {"ecc-v3.1-portable.zip": "manifest-portable.txt",
                      "ecc-v3.1-reasonix-integrated.zip":
                          "manifest-reasonix-integrated.txt"}
    clean = b"print('ok')\n"
    digest = hashlib.sha256(data).hexdigest()
    for zname_i in DELIVERY_ZIPS:
        payload = data if zname_i == zname else clean
        arc_i = arc if zname_i == zname else "ok.py"
        with zipfile.ZipFile(str(d / zname_i), "w",
                             zipfile.ZIP_DEFLATED) as zf:
            info = zipfile.ZipInfo(arc_i, date_time=(1980, 1, 1, 0, 0, 0))
            zf.writestr(info, payload)
        sha = hashlib.sha256(payload).hexdigest()
        (d / manifest_names[zname_i]).write_text(
            f"# m\n{arc_i}\t{len(payload)}\t{sha}\n"
            f"# archive\t{zname_i}\t{(d / zname_i).stat().st_size}\t"
            f"{sha256_file(str(d / zname_i))}\n", encoding="utf-8")
    sha_lines = ["# sha"]
    for name in DELIVERY_ZIPS + DELIVERY_MANIFESTS:
        sha_lines.append(f"{sha256_file(str(d / name))}  {name}")
    (d / "sha256.txt").write_text("\n".join(sha_lines) + "\n",
                                  encoding="utf-8")
    return d


def write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2),
                    encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
