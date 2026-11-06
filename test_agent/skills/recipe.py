"""环境 recipe 校验闸与构建（任务F）。

安全模型（设计 §5）：LLM/探测器给的是"选择题答案"，闸门是确定性的：
1. schema：字段/枚举/类型齐全；
2. base_image 必须在 RECIPE_IMAGE_POOL 里（不能自造镜像名）；
3. install 每条必须全匹配 RECIPE_ALLOWED_INSTALL 白名单（禁 curl|sh、sudo、任意 URL）；
4. 预算：Dockerfile 行数、构建超时、镜像大小；
5. 不通过=整份拒绝（不带着病构建），调用方降级 static_only。

构建：白名单拼装 Dockerfile（FROM base + RUN install + 非 root 用户 + WORKDIR），
docker build → 健康检查（解释器版本 + 测试框架 --version）。
"""
import logging
import re
import time
from pathlib import Path

from .. import config

log = logging.getLogger(__name__)

REQUIRED_FIELDS = ("schema_version", "language", "runtime_version", "base_image",
                   "package_manager", "install", "test_framework")
_ENUMS = {
    "language": ("python", "javascript"),
    "test_framework": ("pytest", "jest"),
}


class RecipeRejected(ValueError):
    """recipe 未过校验闸（消息面向用户，可直接展示）。"""


def validate_recipe(r: dict) -> dict:
    """校验并归一 recipe；不通过抛 RecipeRejected（含具体哪条不合规）。返回清洗后的副本。"""
    if not isinstance(r, dict):
        raise RecipeRejected("recipe 不是对象")
    missing = [f for f in REQUIRED_FIELDS if r.get(f) is None]
    if missing:
        raise RecipeRejected(f"recipe 缺字段：{missing}")
    for field, allowed in _ENUMS.items():
        if r[field] not in allowed:
            raise RecipeRejected(f"{field}={r[field]!r} 不在允许值 {allowed}")
    pool = config.RECIPE_IMAGE_POOL.get(r["language"], {})
    if not pool:
        raise RecipeRejected(f"语言 {r['language']} 无镜像池")
    base = str(r["base_image"])
    if base not in pool.values():
        raise RecipeRejected(
            f"base_image={base!r} 不在镜像池 {sorted(pool.values())}（不允许自造镜像名）")
    ver = str(r["runtime_version"])
    if ver not in pool:
        # 允许 3.11.4 这类精确写法归一到大版本 3.11
        mm = ".".join(ver.split(".")[:2])
        if mm in pool:
            ver = mm
        else:
            raise RecipeRejected(f"runtime_version={ver!r} 不在镜像池版本 {sorted(pool)}")
    # 黑包阶段2：可选 db_services 段（DB sidecar 声明）——存在即校验（缺字段必报错）
    if "db_services" in r and r["db_services"] is not None:
        from .db_sidecar import parse_db_services
        try:
            r["db_services"] = parse_db_services(r["db_services"])
        except ValueError as e:
            raise RecipeRejected(str(e)) from e
    # install 白名单（逐条全匹配）
    install = r["install"]
    if not isinstance(install, list) or not install:
        raise RecipeRejected("install 必须是非空数组")
    if len(install) > 8:
        raise RecipeRejected(f"install 条目过多（{len(install)} > 8）")
    for cmd in install:
        if not isinstance(cmd, str) or not cmd.strip():
            raise RecipeRejected("install 条目必须是非空字符串")
        if any(bad in cmd for bad in ("curl", "wget", "sudo", "&&", "||", ";", "|", ">", "<", "`", "$(")):
            raise RecipeRejected(f"install 含禁用片段：{cmd!r}（禁止下载/串联/重定向）")
        if not any(re.fullmatch(p, cmd.strip()) for p in config.RECIPE_ALLOWED_INSTALL):
            raise RecipeRejected(f"install 不在白名单：{cmd!r}")
    ev = r.get("evidence") or {}
    if not ev.get("detected_from"):
        raise RecipeRejected("evidence.detected_from 为空——没有探测依据的配方不采用（防幻觉）")
    out = dict(r)
    out["runtime_version"] = ver
    out["install"] = [c.strip() for c in install]
    out["timeout_s"] = min(int(r.get("timeout_s") or config.RECIPE_MAX_BUILD_S),
                           config.RECIPE_MAX_BUILD_S)
    return out


def render_dockerfile(r: dict) -> str:
    """白名单拼装 Dockerfile（不执行 recipe 里的任意文本，只拼已校验的字段）。"""
    lines = [
        f"# 由 Test Agent recipe 生成（source={r.get('source', 'unknown')}）",
        f"FROM {r['base_image']}",
        "ENV DEBIAN_FRONTEND=noninteractive PIP_DISABLE_PIP_VERSION_CHECK=1",
        "RUN useradd -m -u 1000 tester",
        "WORKDIR /work",
    ]
    for cmd in r["install"]:
        lines.append(f"RUN {cmd}")
    lines += [
        "RUN mkdir -p /work && chown -R tester:tester /work",
        "USER tester",
        'CMD ["sleep", "infinity"]',
    ]
    if len(lines) > config.RECIPE_MAX_DOCKERFILE_LINES:
        raise RecipeRejected(f"Dockerfile 行数 {len(lines)} 超预算 "
                             f"{config.RECIPE_MAX_DOCKERFILE_LINES}")
    return "\n".join(lines) + "\n"


def recipe_image_tag(r: dict) -> str:
    """镜像 tag = recipe 内容哈希（同配方复用，不同配方不复用）。"""
    import hashlib
    import json
    key = json.dumps({k: r[k] for k in ("language", "runtime_version", "base_image",
                                        "package_manager", "install", "test_framework")},
                     sort_keys=True, ensure_ascii=False)
    return "mta-recipe:" + hashlib.sha256(key.encode()).hexdigest()[:12]


def build_image(r: dict, tag: str, ctx_dir: Path | None = None,
                dockerfile_text: str | None = None, timeout_s: int | None = None):
    """构建 recipe 镜像。返回 (image_obj|None, error_text)。构建失败不抛（调用方决定降级）。"""
    import docker
    from docker.errors import BuildError
    client = docker.from_env()
    try:
        client.images.get(tag)          # 已有同配方镜像 → 直接复用（缓存命中）
        return client.images.get(tag), None
    except Exception:                   # noqa: BLE001 —— ImageNotFound 及驱动差异
        pass
    text = dockerfile_text or render_dockerfile(r)
    ctx_dir = Path(ctx_dir) if ctx_dir else Path(config.DATA_DIR) / "recipe_ctx" / tag.split(":")[-1]
    ctx_dir.mkdir(parents=True, exist_ok=True)
    (ctx_dir / "Dockerfile").write_text(text, encoding="utf-8")
    t0 = time.time()
    try:
        img, logs = client.images.build(path=str(ctx_dir), tag=tag, rm=True,
                                        timeout=int(timeout_s or r.get("timeout_s")
                                                    or config.RECIPE_MAX_BUILD_S))
        # 镜像大小预算
        size_mb = (img.attrs.get("Size") or 0) / (1024 * 1024)
        if size_mb > config.RECIPE_MAX_IMAGE_MB:
            log.warning("recipe 镜像 %s 大小 %.0fMB 超预算 %sMB（不阻断，仅告警）",
                        tag, size_mb, config.RECIPE_MAX_IMAGE_MB)
        log.info("recipe 镜像构建完成 %s（%.0fs, %.0fMB）", tag, time.time() - t0, size_mb)
        return img, None
    except BuildError as e:
        tail = "\n".join(str(x) for x in (e.build_log or [])[-8:]) if e.build_log else ""
        return None, f"构建失败：{e}\n{tail[-800:]}"
    except Exception as e:              # noqa: BLE001 —— 超时/守护进程问题
        return None, f"{type(e).__name__}: {e}"


def health_check(runner, image: str, name: str, r: dict) -> tuple[bool, str]:
    """容器内自检：解释器版本 + 测试框架可用（recipe 声明的环境真的能用吗）。"""
    try:
        c = runner.ensure_container(image, name)
    except Exception as e:              # noqa: BLE001
        return False, f"容器起不来：{type(e).__name__}: {e}"
    try:
        if r["language"] == "python":
            r1 = runner.exec_in(c, ["python", "--version"], timeout_s=30)
            r2 = runner.exec_in(c, ["python", "-m", "pytest", "--version"], timeout_s=60)
            ok = r1.ok and r2.ok
            detail = f"{r1.stdout.strip() or r1.stderr.strip()} / {r2.stdout.strip() or r2.stderr.strip()}"
        else:
            r1 = runner.exec_in(c, ["node", "--version"], timeout_s=30)
            r2 = runner.exec_in(c, ["npx", "--no-install", "jest", "--version"], timeout_s=90)
            ok = r1.ok and r2.ok
            detail = f"{r1.stdout.strip() or r1.stderr.strip()} / {r2.stdout.strip() or r2.stderr.strip()}"
        return ok, detail[:300]
    finally:
        runner.cleanup(name)


def try_build_recipe(r: dict, runner=None) -> dict:
    """编排：校验 → 拼 Dockerfile → 构建 → 健康检查。返回
    {ok, tag, stage, error, recipe}
    stage ∈ validate/dockerfile/build/health；失败时 error 可直接展示给用户。"""
    try:
        clean = validate_recipe(r)
    except RecipeRejected as e:
        return {"ok": False, "stage": "validate", "error": str(e), "recipe": r}
    try:
        text = render_dockerfile(clean)
    except RecipeRejected as e:
        return {"ok": False, "stage": "dockerfile", "error": str(e), "recipe": clean}
    tag = recipe_image_tag(clean)
    img, err = build_image(clean, tag, dockerfile_text=text)
    if img is None:
        return {"ok": False, "stage": "build", "error": err, "recipe": clean, "tag": tag}
    if runner is not None:
        ok, detail = health_check(runner, tag, "mta-healthcheck", clean)
        if not ok:
            return {"ok": False, "stage": "health",
                    "error": f"环境自检未通过：{detail}", "recipe": clean, "tag": tag}
    return {"ok": True, "tag": tag, "recipe": clean, "stage": "done", "error": None}
