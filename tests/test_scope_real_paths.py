"""A3+B 索引加固测试（2026-11-20 设计方案 docs/设计方案-文件级修复与验收设计-20261120.md 包 A3/B）。

论断 3：模块名拼 scope 路径失配——nodes.py 用模块名拼目录（src/user 布局拼出 user/
永远锁空），弃用了 match_requirement 返回的真实文件路径；覆盖率兜底把"范围内缺席+
TOTAL>0"误判 ok。索引三缺口：OpenAPI 解析分支死代码（_walk_src 无 .json）、需求侧
裸子串误路由（log ⊂ login）、ensure_index 缓存无指纹校验（docstring 名不副实）。
零 LLM、零容器、全部 tmp 目录。"""
import json

from test_agent.skills.project_index import (
    INDEX_ALGO,
    _module_name,
    _src_fingerprint,
    _walk_spec_files,
    build_index,
    coverage_check,
    ensure_index,
    match_requirement,
    scope_prefixes_for,
)


def _ws(tmp_path, files: dict[str, str]):
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return tmp_path


# ---------- scope_prefixes_for：真实路径前缀 ----------

def test_scope_prefixes_for_nested_layout():
    idx = {"modules": {"user": {"files": ["src/user/a.py", "src/user/b.py"]}}}
    assert scope_prefixes_for(idx, ["user"]) == ["src/user"], \
        "嵌套布局必须锁真实前缀 src/user（修复前拼模块名 user/ 永远锁空）"


def test_scope_prefixes_for_monorepo_layout():
    idx = {"modules": {"user": {"files": ["packages/user/a.py", "packages/user/sub/b.py"]}}}
    got = scope_prefixes_for(idx, ["user"])
    assert "packages/user" in got, "monorepo 布局必须锁真实前缀 packages/user（含子目录前缀冗余无害）"


def test_scope_prefixes_for_multi_module_and_root():
    idx = {"modules": {"user": {"files": ["user/a.py"]},
                       "auth": {"files": ["src/auth/x.py"]},
                       "_root": {"files": ["main.py"]}}}
    got = scope_prefixes_for(idx, ["user", "auth", "_root"])
    assert got == ["src/auth", "user"], "多模块多前缀；_root 根级文件无目录语义跳过"


def test_scope_prefixes_for_flat_layout_regression():
    """平铺布局（模块名=真实目录段）行为与旧拼名一致（回归锁）。"""
    idx = {"modules": {"user": {"files": ["user/a.py", "user/b.py"]}}}
    assert scope_prefixes_for(idx, ["user"]) == ["user"]


# ---------- match_requirement 词边界（B2） ----------

def _idx_log_login(tmp_path):
    ws = _ws(tmp_path, {"log/audit.py": "def audit(): pass\n",
                        "login/views.py": "def login_view(): pass\n"})
    return build_index(ws, ["python"])


def test_requirement_word_boundary_blocks_log_in_login(tmp_path):
    idx = _idx_log_login(tmp_path)
    hit = match_requirement(idx, "test the login page")
    assert hit["kind"] != "module" or hit["module"] != "log", \
        "需求含 login 不得误命中 log 模块（裸子串 in 拦截）"


def test_requirement_word_boundary_still_hits_exact(tmp_path):
    idx = _idx_log_login(tmp_path)
    hit = match_requirement(idx, "test the log module")
    assert hit["kind"] == "module" and hit["module"] == "log", "全词命中不误杀"


def test_requirement_chinese_substring_semantics_kept(tmp_path):
    # 两个模块：单模块仓文件数=全库不参与路由（E2E-1 设计使然），需第二个模块才可路由
    ws = _ws(tmp_path, {"登录/views.py": "def login(): pass\n",
                        "订单/core.py": "def o(): pass\n"})
    idx = build_index(ws, ["python"])
    hit = match_requirement(idx, "测试登录功能")
    assert hit["kind"] == "module" and hit["module"] == "登录", \
        "中文模块名保持子串语义（无词边界可言）"


# ---------- OpenAPI spec 枚举（B1：死分支复活） ----------

def test_walk_spec_files_finds_openapi_json(tmp_path):
    _ws(tmp_path, {"api/openapi.json": "{}", "node_modules/x/openapi.json": "{}",
                   "readme.json": "{}"})
    got = [p.name for p in _walk_spec_files(tmp_path)]
    assert "openapi.json" in got and len(got) == 1, "node_modules 跳过、非 spec json 不收"


def test_build_index_registers_openapi_endpoints(tmp_path):
    spec = {"openapi": "3.0.0", "paths": {"/pets": {"get": {"summary": "list"}}}}
    ws = _ws(tmp_path, {"openapi.json": json.dumps(spec),
                        "app/main.py": "def x(): pass\n"})
    idx = build_index(ws, ["python"])
    paths = [e.get("path") for e in idx.get("endpoints", [])]
    assert "/pets" in paths, "spec 端点必须注册（修复前解析分支不可达=死代码）"


# ---------- ensure_index 指纹校验（B3） ----------

def test_ensure_index_reuses_when_fingerprint_same(tmp_path):
    ws = _ws(tmp_path, {"user/a.py": "def a(): pass\n"})
    idx1 = ensure_index(9001, ws, ["python"])
    assert idx1.get("index_algo") == INDEX_ALGO and idx1.get("src_fingerprint")
    idx2 = ensure_index(9001, ws, ["python"])
    assert idx2["src_fingerprint"] == idx1["src_fingerprint"], "无变化必须复用"


def test_ensure_index_rebuilds_when_source_added(tmp_path):
    ws = _ws(tmp_path, {"user/a.py": "def a(): pass\n"})
    ensure_index(9002, ws, ["python"])
    (ws / "user" / "b.py").write_text("def b(): pass\n", encoding="utf-8")
    idx2 = ensure_index(9002, ws, ["python"])
    files = idx2["modules"].get("user", {}).get("files", [])
    assert "user/b.py" in files, "新增源码后必须重建（修复前读旧索引=缓存缺口）"


def test_ensure_index_upgrades_legacy_doc(tmp_path):
    """旧格式索引（无 index_algo 键）必须重建（升级自愈）。"""
    ws = _ws(tmp_path, {"user/a.py": "def a(): pass\n"})
    ensure_index(9003, ws, ["python"])
    # 模拟历史旧文档：抹掉版本键
    from test_agent.skills.project_index import _index_dir
    out = _index_dir() / "project_9003.json"
    doc = json.loads(out.read_text(encoding="utf-8"))
    doc.pop("index_algo", None)
    out.write_text(json.dumps(doc), encoding="utf-8")
    idx2 = ensure_index(9003, ws, ["python"])
    assert idx2.get("index_algo") == INDEX_ALGO, "旧格式必须升级重建"


def test_src_fingerprint_sensitivity(tmp_path):
    ws = _ws(tmp_path, {"a.py": "def a(): pass\n"})
    fp1 = _src_fingerprint(ws)
    (ws / "a.py").write_text("def a(): pass\n\ndef b(): pass\n", encoding="utf-8")
    assert _src_fingerprint(ws) != fp1, "内容变更必须改变指纹"


# ---------- gen_tests 零收录回退（A3） ----------

def test_collect_scope_empty_falls_back_to_full(tmp_path):
    from test_agent.skills.gen_tests import _collect_sources_detailed
    ws = _ws(tmp_path, {"src/user/core.py": "def core(): pass\n",
                        "package.json": "{}"})
    sources, skipped = _collect_sources_detailed(ws, "python", scope_paths=["user/"])
    rels = [r for r, _ in sources]
    assert "src/user/core.py" in rels, "零收录必须回退全库（修复前 src/user/core.py 被锁空=0 收录）"
    assert any("[index-scope]" in s for s in skipped), "回退必须台账留痕（不静默）"


def test_collect_scope_lock_still_filters_when_prefix_correct(tmp_path):
    from test_agent.skills.gen_tests import _collect_sources_detailed
    ws = _ws(tmp_path, {"user/core.py": "def core(): pass\n",
                        "other/other.py": "def o(): pass\n"})
    sources, _ = _collect_sources_detailed(ws, "python", scope_paths=["user/"])
    rels = [r for r, _ in sources]
    assert "user/core.py" in rels and "other/other.py" not in rels, \
        "前缀正确时过滤行为不变（回归锁）"


# ---------- coverage_check scope_empty 防御（A3-3b） ----------

def test_coverage_check_scope_count_zero_blocks_false_ok():
    state = {"index_scope_paths": ["user/"], "index_scope_count": 0}
    chk = coverage_check(state, per_file=[], coverage_line=0.5)
    assert chk["verdict"] == "scope_empty", \
        "锁定零收录时 TOTAL>0 不得判 ok（修复前漏掉急救的误判形态）"


def test_coverage_check_legacy_state_unchanged():
    """无 scope_count 键（老链路）行为不变——兼容回归锁。"""
    state = {"index_scope_paths": ["user/"]}
    chk = coverage_check(state, per_file=[], coverage_line=0.5)
    assert chk["verdict"] == "ok"
    state2 = {"index_scope_paths": []}
    assert coverage_check(state2, per_file=[], coverage_line=0.5)["verdict"] == "not_routed"


# ---------- 接线锁 ----------

def test_nodes_wiring_uses_real_paths():
    """nodes.py 两处 scope 构造必须走 scope_prefixes_for（grep 锁——防回归拼模块名）。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "test_agent" / "graph" / "nodes.py"
           ).read_text(encoding="utf-8")
    assert src.count("scope_prefixes_for(") >= 2, "analyze 路由+L2 扩圈两处接线"
    assert '[f"{m}/" for m in closure' not in src, "模块名拼路径必须清零"


def test_nodes_generate_consumes_scope_fallback():
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "test_agent" / "graph" / "nodes.py"
           ).read_text(encoding="utf-8")
    assert "scope_fallback" in src, "generate 必须消费 gen_tests 的零收录回退标（清空路由）"


def test_module_name_exclusion_unchanged():
    """_module_name 排除表行为回归锁（src/user → user，不是 src）。"""
    assert _module_name(__import__("pathlib").Path("src/user/a.py")) == "user"
    assert _module_name(__import__("pathlib").Path("user/a.py")) == "user"
    assert _module_name(__import__("pathlib").Path("main.py")) == "_root"


# ---------- B4：审计故障注入金样（2026-11-20 设计方案 §3 B4） ----------
# 设计纪律：审计器必须能抓到自己人制造的破坏——三注入全部被机制检出（变红），
# 抓不到 = 审计器不合格。金样固化成常驻单测（不依赖真实库/真机索引）。

def test_b4_injection_1_scope_prefix_sabotage(tmp_path):
    """注入①：人为制造错误 scope 前缀（模拟 A3 修前行为）→ 机制必须显式回退留痕。"""
    from test_agent.skills.gen_tests import _collect_sources_detailed
    ws = _ws(tmp_path, {"src/user/core.py": "def core(): pass\n"})
    sources, skipped = _collect_sources_detailed(ws, "python", scope_paths=["user/"])
    assert any(r == "src/user/core.py" for r, _ in sources), "注入①：锁空前缀不得静默吞源码"
    assert any("[index-scope]" in s for s in skipped), "注入①：审计注记必须变红（留痕）"


def test_b4_injection_2_endpoint_removal_detected(tmp_path):
    """注入②：删除 spec 里的一个端点 → 重建后的索引必须如实反映（对账可检出差异）。"""
    spec_full = {"openapi": "3.0.0", "paths": {"/pets": {"get": {}}, "/users": {"get": {}}}}
    ws1 = _ws(tmp_path / "full", {"openapi.json": json.dumps(spec_full),
                                  "app/main.py": "def x(): pass\n"})
    spec_broken = {"openapi": "3.0.0", "paths": {"/pets": {"get": {}}}}
    ws2 = _ws(tmp_path / "broken", {"openapi.json": json.dumps(spec_broken),
                                    "app/main.py": "def x(): pass\n"})
    eps_full = sorted(e.get("path") for e in build_index(ws1, ["python"])["endpoints"])
    eps_broken = sorted(e.get("path") for e in build_index(ws2, ["python"])["endpoints"])
    assert eps_full == ["/pets", "/users"] and eps_broken == ["/pets"], \
        "注入②：端点删除必须在对账中显形（审计变红的前提）"


def test_b4_injection_3_stale_cache_detected(tmp_path):
    """注入③：旧缓存+新源码 → ensure_index 指纹必须触发重建（修复前直接读旧索引）。"""
    ws = _ws(tmp_path, {"user/a.py": "def a(): pass\n"})
    idx1 = ensure_index(9100, ws, ["python"])
    assert "user/a.py" in idx1["modules"]["user"]["files"]
    (ws / "user" / "a.py").write_text("def a(): pass\n\ndef NEW(): pass\n", encoding="utf-8")
    idx2 = ensure_index(9100, ws, ["python"])
    syms = idx2["modules"]["user"].get("symbols", [])
    assert any("NEW" in s for s in syms), "注入③：旧缓存必须被指纹校验识破（重建含新符号）"
