"""环境探测（任务F）：确定性识别语言/版本/包管理器（零 LLM）。

输出喂给 recipe 生成：能定就定（deterministic recipe），歧义才交给 LLM 提案
（提案同样过白名单闸）。设计依据 docs/环境自适应调研与设计.md §3。

证据纪律：每个字段都带 detected_from（哪个文件推出来的），无证据的字段留空——
校验闸拒收"声明了版本但无探测依据"的 recipe（防 LLM 幻觉版本号）。
"""
import json
import re
from pathlib import Path

# 版本文件 → (语言, 取值方式)
_VERSION_FILES = {
    ".python-version": "python",
    ".nvmrc": "javascript",
    ".node-version": "javascript",
}


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _py_requires_from_pyproject(text: str) -> str | None:
    m = re.search(r'requires-python\s*=\s*["\']([^"\']+)["\']', text)
    if not m:
        return None
    req = m.group(1)
    vers = re.findall(r"(\d+\.\d+)", req)
    return vers[-1] if vers else None


def _pm_from_pyproject(text: str) -> str | None:
    if "[tool.poetry" in text:
        return "poetry"
    if "[tool.pdm" in text:
        return "pdm"
    return None


def _ver_from_engine(text: str) -> str | None:
    m = re.search(r'"node"\s*:\s*"([^"]+)"', text)
    if not m:
        return None
    vers = re.findall(r"(\d+)", m.group(1))
    return vers[0] if vers else None


def detect_stack(ws: str | Path) -> dict:
    """探测项目技术栈。返回：
    {language, runtime_version, package_manager, manifests[], lockfiles[],
     evidence{field: [file,...]}, conflicts[]}

    language 取值：python / javascript / unknown（java/go/rust 目前只作识别不接执行）。
    """
    ws = Path(ws)
    evidences: dict[str, list[str]] = {}
    lang_votes: dict[str, int] = {}
    manifests, lockfiles = [], []
    conflict: list[str] = []

    def ev(field: str, src: str) -> None:
        evidences.setdefault(field, []).append(src)

    def vote(lang: str, n: int = 1) -> None:
        lang_votes[lang] = lang_votes.get(lang, 0) + n

    # —— Python ——
    py_version = None
    for f in ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
              "requirements-dev.txt", "Pipfile", "poetry.lock", "pdm.lock"):
        if (ws / f).is_file():
            manifests.append(f)
            vote("python")
            if f.endswith(".lock"):
                lockfiles.append(f)
    if (ws / ".python-version").is_file():
        v = _read(ws / ".python-version").strip().splitlines()
        if v:
            py_version = v[0].strip()
            ev("runtime_version", ".python-version")
    if (ws / "pyproject.toml").is_file():
        t = _read(ws / "pyproject.toml")
        if not py_version:
            got = _py_requires_from_pyproject(t)
            if got:
                py_version = got
                ev("runtime_version", "pyproject.toml[requires-python]")
        pm = _pm_from_pyproject(t)
        if pm:
            ev("package_manager", f"pyproject.toml[{pm}]")
    pm = "poetry" if (ws / "poetry.lock").is_file() or \
        "poetry" in (evidences.get("package_manager") or []) else "pip"
    if (ws / "requirements.txt").is_file():
        ev("package_manager", "requirements.txt")
    if (ws / "Pipfile").is_file():
        pm = "pip"

    # —— JavaScript ——
    js_version = None
    if (ws / "package.json").is_file():
        manifests.append("package.json")
        vote("javascript")
        t = _read(ws / "package.json")
        got = _ver_from_engine(t)
        if got:
            js_version = got
            ev("runtime_version", "package.json[engines.node]")
    for f in ("package-lock.json", "yarn.lock", "pnpm-lock.yaml"):
        if (ws / f).is_file():
            lockfiles.append(f)
            vote("javascript")
    if (ws / ".nvmrc").is_file():
        v = _read(ws / ".nvmrc").strip().splitlines()
        if v:
            js_version = v[0].strip().lstrip("v")
            ev("runtime_version", ".nvmrc")
    # 白包阶段1（TS 升 Tier2）：tsconfig.json = TypeScript 工程特征——归 node 工具链家族
    # （执行镜像同为 node20-jest，散装/带清单两形态见 install_deps 与 _inline_jest_config）
    if (ws / "tsconfig.json").is_file():
        manifests.append("tsconfig.json")
        vote("javascript")
        ev("language", "tsconfig.json（TypeScript 工程特征，node 工具链家族）")

    # —— 其它语言（仅识别，不接执行） ——
    for f, lang in (("pom.xml", "java"), ("build.gradle", "java"),
                    ("go.mod", "go"), ("Cargo.toml", "rust")):
        if (ws / f).is_file():
            manifests.append(f)
            vote(lang)

    # —— 决策 ——
    if lang_votes.get("javascript", 0) > lang_votes.get("python", 0):
        language, runtime_version = "javascript", js_version
        package_manager = ("pnpm" if "pnpm-lock.yaml" in lockfiles else
                           "yarn" if "yarn.lock" in lockfiles else "npm")
        if "package-lock.json" in lockfiles:
            ev("package_manager", "package-lock.json")
    elif lang_votes.get("python", 0) > 0:
        language, runtime_version = "python", py_version
        package_manager = pm
    elif lang_votes:
        language = max(lang_votes, key=lang_votes.get)
        runtime_version = None
        package_manager = ""
    else:
        language, runtime_version, package_manager = "unknown", None, ""

    if lang_votes.get("python", 0) and lang_votes.get("javascript", 0):
        conflict.append(f"同时存在 python({lang_votes['python']}) 与 "
                        f"javascript({lang_votes['javascript']}) 清单，按投票取多数")
    if language == "unknown":
        conflict.append("未发现任何可识别的依赖清单")

    return {
        "language": language,
        "runtime_version": (runtime_version or "").strip() or None,
        "package_manager": package_manager,
        "manifests": sorted(set(manifests)),
        "lockfiles": sorted(set(lockfiles)),
        "evidence": evidences,
        "conflicts": conflict,
    }


def deterministic_recipe(stack: dict) -> dict | None:
    """由探测结果直接产 recipe（零 LLM）。信息不足（语言不可执行/无版本）时返回 None
    → 由调用方决定走 LLM 提案或降级 static_only。"""
    from .. import config
    lang, ver, pm = stack.get("language"), stack.get("runtime_version"), stack.get("package_manager")
    if lang not in config.RECIPE_IMAGE_POOL:
        return None
    pool = config.RECIPE_IMAGE_POOL[lang]
    top = max(pool, key=lambda v: tuple(int(x) for x in v.split(".") if x.isdigit()))
    # 版本匹配：精确 → 主版本 → 池内最高（并记录 assumption）
    note = None
    if ver and ver in pool:
        base, use_ver = pool[ver], ver
    elif ver:
        major_minor = ".".join(ver.split(".")[:2])
        major = ver.split(".")[0]
        if major_minor in pool:
            base, use_ver = pool[major_minor], major_minor
        elif major in pool:
            base, use_ver = pool[major], major
        else:
            base, use_ver = pool[top], top
            note = f"项目声明 {ver} 不在镜像池，回退到池内 {top}（可能不兼容）"
    else:
        base, use_ver = pool[top], top
        note = f"未探测到版本，默认 {top}"
    install: list[str] = []
    if lang == "python":
        if "requirements.txt" in stack.get("manifests", []):
            install.append("pip install --no-cache-dir -r requirements.txt")
            if "requirements-dev.txt" in stack["manifests"]:
                install.append("pip install --no-cache-dir -r requirements-dev.txt")
        elif pm == "poetry" and "pyproject.toml" in stack.get("manifests", []):
            install.append("pip install poetry && poetry install --no-root")
        elif "pyproject.toml" in stack.get("manifests", []) or \
                "setup.py" in stack.get("manifests", []):
            install.append("pip install --no-cache-dir .")
        install.append("pip install pytest pytest-cov")
    else:
        if "package-lock.json" in stack.get("lockfiles", []):
            install.append("npm ci")
        elif "pnpm-lock.yaml" in stack.get("lockfiles", []):
            install.append("pnpm install --frozen-lockfile --ignore-scripts")
        elif "yarn.lock" in stack.get("lockfiles", []):
            install.append("yarn install --frozen-lockfile --ignore-scripts")
        else:
            install.append("npm install --ignore-scripts")
        install.append("npm install --no-save --ignore-scripts jest")
    return {
        "schema_version": 1,
        "language": lang,
        "runtime_version": str(use_ver),
        "base_image": base,
        "package_manager": pm or ("pip" if lang == "python" else "npm"),
        "install": install,
        "test_framework": "pytest" if lang == "python" else "jest",
        "timeout_s": 900,
        "evidence": {"detected_from": sorted(set(
            stack.get("manifests", []) + stack.get("lockfiles", [])
            + [v for vs in (stack.get("evidence") or {}).values() for v in vs])),
            "confidence": "high" if stack.get("runtime_version") else "medium",
            "note": note},
        "source": "deterministic",
    }
