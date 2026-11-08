# -*- coding: utf-8 -*-
"""Go 用例漏 import 自检自补 + 编译错误 stderr 留痕（2026-11-08 658 实锤）：

LAYER_AWARE 对照格 658：AI 用例写出全部四条检出方向断言（404/非5xx、400+一致性、
分页对账、JWT 72h）但漏 import encoding/base64 → 整包 build failed → 8 条用例
0 条执行 → 检出率假 0；且 go_adapter 的 build_err 恒空（编译错误走 stderr 不产生
-json 事件）→ message 只有 "build failed"=证据蒸发（task_366 stderr 丢弃同族）。"""
from test_agent.skills.gen_tests import _fix_go_missing_imports, _strip_go_undefined_calls
from test_agent.skills.go_adapter import go_test_json_to_rows


def _mk_go(imports: str, body: str) -> str:
    return (f"// AI 生成用例\npackage main\n\nimport (\n{imports}\n)\n\n{body}\n")


# ---------- 658 重放：漏 base64 → 精确补 encoding/base64 ----------

def test_658重放_漏base64自动补():
    imports = '\t"bytes"\n\t"encoding/json"\n\t"net/http"\n\t"net/http/httptest"\n\t"strings"\n\t"testing"\n\t"time"'
    body = ('func TestR1_Login_Token(t *testing.T) {\n'
            '\tclaimsBytes, err := base64.RawURLEncoding.DecodeString(parts[1])\n'
            '\tif err != nil {\n\t\tt.Fatalf("decode claims: %v", err)\n\t}\n}')
    fixed, added = _fix_go_missing_imports(_mk_go(imports, body))
    assert added == ['"encoding/base64"']              # 补缺的这一个
    assert '"encoding/base64"' in fixed
    # 双向（660 扩展）：body 只用 base64+testing——未使用的 bytes/json/http/httptest/strings/time 全删
    assert '"testing"' in fixed
    for gone in ('"bytes"', '"encoding/json"', '"net/http"', '"net/http/httptest"', '"strings"', '"time"'):
        assert gone not in fixed


def test_已import不重复补():
    imports = '\t"encoding/base64"\n\t"testing"'
    body = 'func TestX(t *testing.T) {\n\t_ = base64.StdEncoding\n}'
    fixed, added = _fix_go_missing_imports(_mk_go(imports, body))
    assert added == [] and fixed.count('"encoding/base64"') == 1


def test_struct_tag不误触发():
    # struct tag `json:"name"` 是冒号不是点调用——不补 encoding/json
    imports = '\t"testing"'
    body = ('func TestX(t *testing.T) {\n'
            '\tvar resp struct {\n\t\tName string `json:"name"`\n\t}\n\t_ = resp\n}')
    fixed, added = _fix_go_missing_imports(_mk_go(imports, body))
    assert added == [] and '"encoding/json"' not in fixed


def test_无import块原样返回():
    fixed, added = _fix_go_missing_imports("package main\nfunc TestX(t *testing.T) {}")
    assert added == [] and fixed == "package main\nfunc TestX(t *testing.T) {}"


def test_659重放_项目内包占用同包名时禁补():
    # go-rest-api 的 internal/errors 与标准库 errors 同包名——LLM 已 import 项目包，
    # body 的 errors. 命中映射表但补标准库=errors redeclared 整包死（659 真机实锤）
    imports = '\t"github.com/qiangxue/go-rest-api/internal/errors"\n\t"testing"'
    body = 'func TestX(t *testing.T) {\n\tif err != nil {\n\t\tt.Error(errors.New("x"))\n\t}\n}'
    fixed, added = _fix_go_missing_imports(_mk_go(imports, body))
    assert '"errors"' not in added                    # 标准库 errors 被项目包占用→禁补
    assert fixed.count('"github.com/qiangxue/go-rest-api/internal/errors"') == 1


def test_660重放_unused_import删除():
    # 660 真机实锤："context" imported and not used → 整包 build failed（Go 特有坑：
    # unused import 是编译错误）——LLM 从其他语言习惯带多余 import
    imports = '\t"context"\n\t"encoding/json"\n\t"testing"'
    body = ('func TestX(t *testing.T) {\n'
            '\tvar resp struct{ Name string `json:"name"` }\n'
            '\t_ = resp\n}')                          # json 只出现在 tag（冒号）→json 包零使用
    fixed, added = _fix_go_missing_imports(_mk_go(imports, body))
    assert '"context"' not in fixed                   # 零使用的 context 被删
    assert '"encoding/json"' not in fixed             # tag 冒号不算使用→encoding/json 也删
    assert '"testing"' in fixed                       # 有使用（testing.T）保留
    assert added == []


def test_used_import保留():
    imports = '\t"context"\n\t"encoding/json"\n\t"testing"'
    body = ('func TestX(t *testing.T) {\n'
            '\t_ = context.Background()\n'
            '\tvar resp struct{ Name string `json:"name"` }\n'
            '\tif err := json.Unmarshal(nil, &resp); err != nil { t.Fatal(err) }\n}')
    fixed, added = _fix_go_missing_imports(_mk_go(imports, body))
    assert '"context"' in fixed and '"encoding/json"' in fixed
    assert added == []


# ---------- 幻觉辅助函数手术（668 重放） ----------

def test_668重放_幻觉函数用例剔除():
    # 668 真机实锤：用例调用 newFakeRepo() 却未定义→undefined 整包 build failed
    imports = '\t"testing"'
    body = ('func TestGenR1_CreateAlbum_等价类划分(t *testing.T) {\n'
            '\tsvc := NewService(newFakeRepo(), nil)\n'
            '\tif svc == nil {\n\t\tt.Error("nil")\n\t}\n'
            '}\n\n'
            'func TestGenR1_CreateAlbum_边界值分析(t *testing.T) {\n'
            '\tif 1 != 1 {\n\t\tt.Error("x")\n\t}\n'
            '}\n\n'
            'func newFakeRepo() int { return 0 }\n')   # 场景 A：定义了→不算幻觉
    sources = "package album\n\nfunc NewService(r Repository, l log.Logger) *Service { return nil }\n"
    fixed, stripped = _strip_go_undefined_calls(_mk_go(imports, body), sources)
    assert stripped == []                             # 本文件+项目双白名单全命中，全保留


def test_项目内函数白名单不误杀():
    # 668 复盘：NewService 是被测源码定义的项目函数——sources 白名单必须覆盖
    imports = '\t"testing"'
    body = ('func TestGenR1_A(t *testing.T) {\n'
            '\tsvc := NewService(newFakeRepo(), nil)\n'   # newFakeRepo 项目与文件都没有→幻觉
            '\t_ = svc\n'
            '}\n\n'
            'func TestGenR1_B(t *testing.T) {\n'
            '\tsvc := NewService(nil, nil)\n'             # NewService 项目内有定义→合法
            '\t_ = svc\n'
            '}\n')
    sources = "package album\n\nfunc NewService(r Repository, l log.Logger) *Service { return nil }\n"
    fixed, stripped = _strip_go_undefined_calls(_mk_go(imports, body), sources)
    assert stripped == ["TestGenR1_A"]                # 只删幻觉那个
    assert "TestGenR1_B" in fixed                     # 项目函数调用保留


def test_幻觉函数真剔除():
    imports = '\t"testing"'
    body = ('func TestGenR1_A(t *testing.T) {\n'
            '\tsvc := NewService(newFakeRepo(), nil)\n'
            '\t_ = svc\n'
            '}\n\n'
            'func TestGenR1_B(t *testing.T) {\n'
            '\tif 1 != 1 {\n\t\tt.Error("x")\n\t}\n'
            '}\n')                                     # 场景 B：newFakeRepo 未定义
    fixed, stripped = _strip_go_undefined_calls(_mk_go(imports, body))
    assert stripped == ["TestGenR1_A"]                # 只删含幻觉调用的那个
    assert "TestGenR1_A" not in fixed
    assert "TestGenR1_B" in fixed                     # 好用例保留
    assert "newFakeRepo" not in fixed


def test_方法调用与框架调用不误杀():
    imports = '\t"net/http/httptest"\n\t"testing"'
    body = ('func TestGenR1_HTTP(t *testing.T) {\n'
            '\tw := httptest.NewRecorder()\n'          # pkg.Func 非裸调用
            '\tw.Code = 404\n'
            '\tif w.Code != 404 {\n\t\tt.Errorf("got %d", w.Code)\n\t}\n'
            '}\n')
    fixed, stripped = _strip_go_undefined_calls(_mk_go(imports, body))
    assert stripped == [] and "TestGenR1_HTTP" in fixed


# ---------- go_adapter：编译错误 stderr 留痕（证据蒸发修复） ----------

_JSON_BUILD_FAILED = (
    '{"Action":"output","Package":"example.com/app","Output":"FAIL example.com/app [build failed]\\n"}\n'
)


def test_stderr编译错误进message():
    rows = go_test_json_to_rows(_JSON_BUILD_FAILED,
                                "vet: ./TestGenR1_test.go:245:22: undefined: base64\n")
    err = [r for r in rows if r["name"] == "[build failed]"]
    assert len(err) == 1
    assert "undefined: base64" in err[0]["message"]     # 不再是裸 "build failed"


def test_stdout散行编译错误进message():
    # 659 实锤：build_run_cmd_go 带 2>&1——编译错误混在 stdout 的非 JSON 散行里
    rows = go_test_json_to_rows(
        _JSON_BUILD_FAILED + "vet: ./TestGenFb1_test.go:15:2: errors redeclared in this block\n")
    err = [r for r in rows if r["name"] == "[build failed]"]
    assert "errors redeclared" in err[0]["message"]


def test_无stderr兜底不变():
    rows = go_test_json_to_rows(_JSON_BUILD_FAILED)
    err = [r for r in rows if r["name"] == "[build failed]"]
    assert err[0]["message"] == "build failed"          # 回归保护：无 stderr 时原行为
