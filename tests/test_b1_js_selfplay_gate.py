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
    "code": ('const qs = getQuestions();\n'
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
