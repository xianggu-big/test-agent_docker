"""嵌套项目根上提回归锁（2026-11-17 multi60 卡 2，三族 12 格实锤）。

java petclinic 1272-1277（codeload zip 嵌套 pom）/php demo-main·laravel 1286/1287
（嵌套 composer.json → 跳装 → vendor 缺失）/ts nestjs 1261-1263（嵌套 package.json →
误判 ts_loose 不装依赖）——同一族布局问题，clone 落位后上提一治全治。
"""
from pathlib import Path

from test_agent.skills.nested_root import lift_nested_project_root


def _mk(ws: Path, rel: str, content: str = "x") -> Path:
    p = ws / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def test_lift_nested_java_project(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _mk(ws, "spring-petclinic-main/pom.xml", "<project/>")
    _mk(ws, "spring-petclinic-main/src/App.java")
    _mk(ws, "spring-petclinic-main/README.md")
    msg = lift_nested_project_root(ws)
    assert msg and "嵌套项目根上提" in msg and "pom.xml" in msg
    assert (ws / "pom.xml").exists() and (ws / "src" / "App.java").exists()
    assert not (ws / "spring-petclinic-main").exists(), "空壳子目录应移除"


def test_lift_nested_composer_project(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _mk(ws, "demo-main/composer.json", "{}")
    _mk(ws, "demo-main/src/App.php")
    msg = lift_nested_project_root(ws)
    assert msg and (ws / "composer.json").exists() and (ws / "src" / "App.php").exists()


def test_lift_nested_package_json_project(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _mk(ws, "nestjs-app/package.json", "{}")
    _mk(ws, "nestjs-app/src/main.ts")
    msg = lift_nested_project_root(ws)
    assert msg and (ws / "package.json").exists() and (ws / "src" / "main.ts").exists()


def test_no_lift_when_root_has_manifest(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _mk(ws, "pom.xml")
    _mk(ws, "nested/pom.xml")
    assert lift_nested_project_root(ws) is None
    assert (ws / "nested" / "pom.xml").exists(), "正常布局不得动"


def test_no_lift_when_ambiguous(tmp_path):
    """两个子目录各有清单（gson 无根 pom 的多模块形态）——不猜，如实不上提。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    _mk(ws, "gson/pom.xml")
    _mk(ws, "extras/pom.xml")
    assert lift_nested_project_root(ws) is None
    assert (ws / "gson" / "pom.xml").exists() and (ws / "extras" / "pom.xml").exists()


def test_no_lift_when_no_manifest_anywhere(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _mk(ws, "main.py")
    assert lift_nested_project_root(ws) is None


def test_skip_dirs_not_counted_as_manifest_holders(tmp_path):
    """node_modules/package.json 是依赖清单不是项目根（重跑工作区残留场景）。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    _mk(ws, "node_modules/somepkg/package.json")
    assert lift_nested_project_root(ws) is None


def test_collision_skipped_and_recorded(tmp_path):
    """同名冲突逐项跳过并留痕，子目录保留现场（2026-11-15 五轮找bug 快照语义对齐）。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "README.md").write_text("root leftover", encoding="utf-8")
    _mk(ws, "proj/pom.xml")
    _mk(ws, "proj/README.md", "nested")
    _mk(ws, "proj/src/App.java")
    msg = lift_nested_project_root(ws)
    assert msg and "同名冲突跳过" in msg
    assert (ws / "pom.xml").exists() and (ws / "src" / "App.java").exists()
    assert (ws / "README.md").read_text(encoding="utf-8") == "root leftover", "残留文件不覆盖"
    assert (ws / "proj" / "README.md").exists(), "冲突项保留在原子目录"


def test_real_gson_layout_root_pom_no_lift(tmp_path):
    """真 gson 布局（根有聚合 pom + 多模块子目录）零行为变化。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    _mk(ws, "pom.xml")
    _mk(ws, "gson/pom.xml")
    _mk(ws, "extras/pom.xml")
    assert lift_nested_project_root(ws) is None
