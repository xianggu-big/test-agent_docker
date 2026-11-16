"""嵌套项目根上提（2026-11-17 multi60 卡 2，三族 12 格实锤）。

根因家族：工作区根没有构建清单，真正的项目根在唯一一层子目录里——
- java petclinic 族（1272-1277）：codeload zip 解包出 `spring-petclinic-microservices-main/`
  子目录，pom.xml 在里面 → maven 在 /work 根跑 → MissingProjectException → 0 收集熔断；
- php demo-main/laravel（1286/1287）：composer.json 在子目录 → probe_deps_manifest 返回
  None → "未发现依赖清单，跳过安装" → vendor 缺失 → phpunit 缺失 0 收集熔断；
- ts nestjs 族（1261-1263）：package.json 在子目录 → 误判 ts_loose 只补工具链不装项目
  依赖 → TS2307 Cannot find module '@nestjs/common' → collect 整案废案熔断。

修法选型：不在 6 处命令拼 `cd`（install/run/collect/预热/生成物落位/L2/L3 各自为政，
漏一处就复发），而是 clone 落位后**把项目根整体上提到工作区根**——下游全部零改动。

上提判据（不猜，宁缺勿错）：
- 根已有任一清单 → 不上提（正常布局）；
- 恰好一个（跳过 SKIP_DIRS 后）一层子目录含清单 → 上提该子目录全部内容；
- 0 个或 ≥2 个子目录含清单 → 不上提（歧义布局，多模块无聚合根等，如实不猜）；
- 同名冲突逐项跳过并留痕（工作区残留文件不覆盖）。
"""
import shutil
from pathlib import Path

from .. import config

# 构建清单清单（十种主流形态；Cargo 为范围外语言但识别无害——上提同样改善布局）
MANIFEST_NAMES = ("pom.xml", "build.gradle", "build.gradle.kts", "composer.json",
                  "package.json", "go.mod", "requirements.txt", "pyproject.toml",
                  "setup.py", "Cargo.toml")


def lift_nested_project_root(ws: Path) -> str | None:
    """检测并执行嵌套项目根上提；返回留痕消息（None=未上提，布局正常或歧义不猜）。"""
    ws = Path(ws)
    if not ws.is_dir():
        return None
    if any((ws / n).exists() for n in MANIFEST_NAMES):
        return None                                   # 根已有清单：正常布局
    hits: list[tuple[Path, str]] = []
    try:
        children = sorted(ws.iterdir())
    except OSError:
        return None
    for child in children:
        if not child.is_dir() or child.name in config.SKIP_DIRS:
            continue
        found = [n for n in MANIFEST_NAMES if (child / n).is_file()]
        if found:
            hits.append((child, "/".join(found)))
    if len(hits) != 1:
        return None                                   # 歧义布局：不猜，如实不上提
    sub, found = hits[0]
    moved: list[str] = []
    skipped: list[str] = []
    for entry in sorted(sub.iterdir()):
        target = ws / entry.name
        if target.exists():
            skipped.append(entry.name)                # 工作区残留同名项不覆盖
            continue
        try:
            shutil.move(str(entry), str(target))
        except OSError:
            skipped.append(entry.name)
            continue
        moved.append(entry.name)
    if not moved:
        return None
    note = (f"[clone] 嵌套项目根上提：{sub.name}/ → 工作区根"
            f"（清单 {found}，移动 {len(moved)} 项）"
            "——下游 install/run/collect/预热全部以项目根执行")
    if skipped:
        note += f"；同名冲突跳过 {len(skipped)} 项：{'、'.join(skipped[:5])}"
    # 空壳子目录移除（仅当真空——有冲突残留时保留现场）
    try:
        if not skipped and not any(sub.iterdir()):
            sub.rmdir()
    except OSError:
        pass
    return note
