"""B1（任务卡-369 缺陷修复-20261105）：JS 自导自演文本启发式闸。

369 实锤：4 条失败用例全部通过 vm.runInContext("QUESTIONS = QUESTIONS.filter(...)", ctx)
对被测数据文件的全大写导出常量重新赋值（const 绑定 → TypeError: Assignment to constant
variable，被测删除功能 app.js:724 零执行）——「用例自己改数据自己断言」的自导自演形态。

旗标 DEFENSE_V2_ENABLED 默认关（_static_filter JS 分支逐字节不变）；开=文本启发式剔除，
正常调用型用例（vm 调用被测函数/断言返回值）不误杀。拦截率由 F4 红队格度量（启发式诚实边界）。
"""
from test_agent import config
from test_agent.skills.gen_tests import _js_selfplay_filter, _static_filter

# 369 四条 TypeError 用例的真实代码形态（test_cases 表 369 任务原文摘录）
CASE_VM_REASSIGN = {
    "name": "test_题库_删除不存在的id不误删其他记录_错误推测法",
    "code": ('const src = fs.readFileSync(path.join(__dirname, \'data/questions.js\'), \'utf8\');\n'
             'const ctx = vm.createContext({ console });\n'
             'vm.runInContext(src, ctx);\n'
             'const before = vm.runInContext(\'QUESTIONS.length\', ctx);\n'
             'vm.runInContext("QUESTIONS = QUESTIONS.filter(q => q.id !== \'not-exist-999\')", ctx);\n'
             'const after = vm.runInContext(\'QUESTIONS.length\', ctx);\n'
             'expect(after).toBe(before);'),
}
CASE_VM_REASSIGN_2 = {
    "name": "test_题库_重复删除同一记录第二次无副作用_边界值分析",
    "code": ('vm.runInContext(src, ctx);\n'
             'const n0 = vm.runInContext(\'QUESTIONS.length\', ctx);\n'
             'vm.runInContext("QUESTIONS = QUESTIONS.filter(q => q.id !== \'r2020-02\')", ctx);\n'
             'expect(vm.runInContext(\'QUESTIONS.length\', ctx)).toBe(n0 - 1);'),
}
CASE_NORMAL_VM_CALL = {
    "name": "test_registerSubject_有效科目包注册成功_等价类划分",
    "code": ('const ctx = vm.createContext({ console });\n'
             'vm.runInContext(src, ctx);\n'
             'const ok = vm.runInContext(\'registerSubject(validPack)\', ctx);\n'
             'expect(ok).toBe(true);'),
}
CASE_NORMAL_CALL = {
    "name": "test_删除已存在记录后不可查询_等价类划分",
    # 369 fb1 真实形态：先 readFileSync+vm 加载被测数据文件，再调用断言（产品引用通道在头部）
    "code": ('const src = fs.readFileSync(path.join(__dirname, \'data/questions.js\'), \'utf8\');\n'
             'const ctx = vm.createContext({ console });\n'
             'vm.runInContext(src, ctx);\n'
             'const qs = vm.runInContext(\'QUESTIONS\', ctx);\n'
             'const idx = qs.findIndex(q => q.id === \'r2020-01\');\n'
             'expect(qs.find(q => q.id === \'r2020-01\')).toBeUndefined();'),
}


def test_369_vm_reassign_cases_rejected():
    kept, rejected = _js_selfplay_filter([CASE_VM_REASSIGN, CASE_VM_REASSIGN_2])
    assert kept == []
    assert len(rejected) == 2
    assert "vm 脚本内对全大写导出标识符重新赋值" in rejected[0]


def test_normal_calling_cases_pass():
    kept, rejected = _js_selfplay_filter([CASE_NORMAL_VM_CALL, CASE_NORMAL_CALL])
    assert len(kept) == 2 and rejected == []      # 不误杀调用型用例


def test_top_level_redeclare_and_rechain_detected():
    redecl = {"name": "t1", "code": "const QUESTIONS = load();\nexpect(QUESTIONS.length).toBe(47);"}
    rechain = {"name": "t2", "code": "QUESTIONS = QUESTIONS.filter(q => q.bad);"}
    kept, rejected = _js_selfplay_filter([redecl, rechain])
    assert kept == [] and len(rejected) == 2


def _wrap(body: str) -> str:
    """_static_filter 的 JS 形态判定靠 JS_CASE_RE（test("test_x" 声明），样本需完整用例。"""
    return 'test("test_x_等价类划分", () => {' + body + '});'


def test_gate_off_byte_identical(tmp_path, monkeypatch):
    """旗标关（默认）：JS 分支跳过静态闸，原口径逐字节不变。"""
    monkeypatch.setattr(config, "DEFENSE_V2_ENABLED", False)
    full = dict(CASE_VM_REASSIGN, code=_wrap(CASE_VM_REASSIGN["code"]))
    kept, rejected = _static_filter([full], tmp_path)
    assert kept == [full] and rejected == []


def test_gate_on_filters(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DEFENSE_V2_ENABLED", True)
    bad = dict(CASE_VM_REASSIGN, code=_wrap(CASE_VM_REASSIGN["code"]))
    good = dict(CASE_NORMAL_CALL, code=_wrap(CASE_NORMAL_CALL["code"]))
    kept, rejected = _static_filter([bad, good], tmp_path)
    assert [c["name"] for c in kept] == [good["name"]]
    assert len(rejected) == 1


# ---------- 415 补闸：零产品引用自包含用例（2026-11-06） ----------

# 415 BUG-01 真实形态（task_415 test_cases 原文摘录）：login/register/sanitize 全内联，
# require/readFileSync/vm/fetch 零命中——11 条同族"通过"全是空洞通过
CASE_415_SELFCONTAINED = {
    "name": "test_登录注册_特殊字符与注入攻击_错误推测法",
    "code": ('const users = { alice: \'Passw0rd\' };\n'
             'const sanitize = s => typeof s === \'string\' && !/[<>\'";]|--|\\bOR\\b/i.test(s);\n'
             'const register = (u, p) => {\n'
             '  if (!u || !p) return { ok: false, msg: \'用户名或密码不能为空\' };\n'
             '  if (!sanitize(u) || !sanitize(p)) return { ok: false, msg: \'输入含非法字符\' };\n'
             '  if (u in users) return { ok: false, msg: \'用户名已存在\' };\n'
             '  users[u] = p;\n'
             '  return { ok: true };\n'
             '};\n'
             'const attacks = ["\' OR \'1\'=\'1", \'a\'.repeat(10000)];\n'
             'for (const a of attacks) {\n'
             '  expect(register(a, a).ok).toBe(false);\n'
             '}'),
}


def test_415_selfcontained_mock_rejected():
    """415 形态：零产品引用的内联 mock 用例整案剔除（哪怕它"会通过"）。"""
    kept, rejected = _js_selfplay_filter([CASE_415_SELFCONTAINED])
    assert kept == [] and len(rejected) == 1
    assert "自包含用例" in rejected[0]


def test_415_zero_ref_not_misjudged_by_other_rules():
    """415 用例不命中既有三形态（无全大写重组/vm 重赋值）——证明需要第 4 条规则。"""
    kept, rejected = _js_selfplay_filter([CASE_415_SELFCONTAINED])
    assert rejected and "重组" not in rejected[0] and "vm" not in rejected[0]


def test_require_reference_passes_zero_ref_gate():
    """require 引用被测文件的正常用例不误杀（369 fb1 vm 沙箱用例同族）。"""
    legit = {"name": "t_ok",
             "code": "const calc = require('./calc.js');\nexpect(calc.add(1, 2)).toBe(3);"}
    kept, rejected = _js_selfplay_filter([legit])
    assert kept == [legit] and rejected == []


def test_419_imports_field_require_passes():
    """419 实锤形态：require 写在脚本级共享 imports 字段而非用例体——零引用判定
    必须合并扫描，否则 7 条真实触达产品的好用例被误杀（body 单独看是零引用）。"""
    body_only = {"name": "test_registerSubject_判定表",
                 "code": "expect(registry.registerSubject(null).ok).toBe(false);"}
    kept, rejected = _js_selfplay_filter([body_only])                       # 不传 imports=旧口径误杀
    assert kept == [] and "自包含" in rejected[0]
    kept2, rejected2 = _js_selfplay_filter(
        [body_only], "const registry = require('./data/subjects/_registry.js');")
    assert kept2 == [body_only] and rejected2 == []                        # 合并扫描后放行


def test_python_selfcontained_rejected_flag_on(tmp_path, monkeypatch):
    """Python 对偶闸：无任何指向被测模块 import 的内联用例剔除（旗标开）。"""
    import textwrap
    from test_agent.skills.gen_tests import _static_filter
    monkeypatch.setattr(config, "DEFENSE_V2_ENABLED", True)
    ws = tmp_path
    (ws / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    inline = {"name": "t1", "code": textwrap.dedent("""
        def my_add(a, b):
            return a + b

        def test_t1():
            assert my_add(1, 2) == 3
    """)}
    ok, rejected = _static_filter([inline], ws)
    assert not ok and any("自包含" in r for r in rejected)


def test_python_selfcontained_passes_flag_off(tmp_path, monkeypatch):
    import textwrap
    from test_agent.skills.gen_tests import _static_filter
    monkeypatch.setattr(config, "DEFENSE_V2_ENABLED", False)
    ws = tmp_path
    (ws / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    inline = {"name": "t1", "code": textwrap.dedent("""
        def my_add(a, b):
            return a + b

        def test_t1():
            assert my_add(1, 2) == 3
    """)}
    ok, rejected = _static_filter([inline], ws)
    assert ok and not rejected          # 旗标关=旧行为不变


# ---------- F4 红队补闸：Python 自导自演（_mutates_sut_state，2026-11-06） ----------

def test_mutates_sut_state_assign_form():
    import ast
    from test_agent.skills.gen_tests import _mutates_sut_state
    tree = ast.parse("import mymod\nmymod.QUESTIONS = [1]\n")
    assert _mutates_sut_state(tree, {"mymod": set()})


def test_mutates_sut_state_mutation_call_form():
    """F4 二轮实锤：mymod.DATA.clear() 是方法调用变异非赋值——首轮闸漏过后补 Call 分支。"""
    import ast
    from test_agent.skills.gen_tests import _mutates_sut_state
    tree = ast.parse("import mymod\nmymod.DATA.clear()\n")
    assert _mutates_sut_state(tree, {"mymod": set()})


def test_mutates_sut_state_ignores_own_locals():
    import ast
    from test_agent.skills.gen_tests import _mutates_sut_state
    tree = ast.parse("def test_x():\n    obj = Loader()\n    obj.data.clear()\n")
    assert _mutates_sut_state(tree, {"mymod": set()}) == []   # 用例自有对象不误杀
