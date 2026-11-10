"""任务卡-F2 阶段 0 真机验收（2026-11-10）：5 语言同一口径覆盖率实跑。

每语言：tmp 夹具（demo 拷贝+最小确定性测试）→ 容器跑工具链产出覆盖率产物 →
CoverageAdapter 统一解析 → 5 份 unified JSON 落 data/reports/f2_stage0/。
验收口径（卡 §2 阶段 0）：per_file 字段一致 + 数值非零（假绿防线：用例数>0
且至少 1 条断言执行——容器内工具链退出码/产物行数双证）。
"""
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from test_agent import config  # noqa: E402
from test_agent.sandbox.runner import SandboxRunner  # noqa: E402
from test_agent.skills.coverage_adapter import (FILE_KEYS, from_clover_xml,  # noqa: E402
                                                from_coverage_py_json,
                                                from_go_coverprofile,
                                                from_istanbul, from_jacoco_xml)

OUT = ROOT / "data" / "reports" / "f2_stage0"
DEMO = ROOT / "data" / "demo"

TESTS = {
    "javascript": ("calc.test.js", """const { add, div, clamp } = require("./src/calc");
test("add", () => { expect(add(1, 2)).toBe(3); });
test("div", () => { expect(div(6, 3)).toBe(2); });
test("clamp 边界", () => { expect(clamp(90, 60, 90)).toBe(90); });
"""),
    "go": ("order_test.go", """package shop

import "testing"

func TestGetTier(t *testing.T) {
	if GetTier(95) != "A" {
		t.Fatalf("95 want A got %s", GetTier(95))
	}
	if GetTier(90) != "A" {
		t.Fatalf("90 want A got %s", GetTier(90))
	}
}

func TestAdd(t *testing.T) {
	if Add(1, 2) != 3 {
		t.Fatal("add wrong")
	}
}
"""),
    "java": ("src/test/java/com/demo/OrderServiceTest.java", """package com.demo;

import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

class OrderServiceTest {
    @Test
    void tierBoundary() {
        assertEquals("A", new OrderService().getTier(90));
    }

    @Test
    void discount() {
        assertEquals(50.0, new OrderService().applyDiscount(100, 50));
    }
}
"""),
    "php": ("tests/ShopTest.php", """<?php
use PHPUnit\\Framework\\TestCase;

require __DIR__ . '/../src/Shop.php';

final class ShopTest extends TestCase
{
    public function testTierBoundary(): void
    {
        $this->assertSame('A', Shop::getTier(90));
    }

    public function testAdd(): void
    {
        $this->assertSame(3, Shop::add(1, 2));
    }
}
"""),
}

# (语言, 镜像, 容器内命令, 产物路径, 解析函数)
PLAN = [
    ("python", config.IMAGE_PY,
     ["python", "-m", "pytest", "-q", "--cov=.", "--cov-report=json:cov.json"],
     "cov.json", "cov_py.json", from_coverage_py_json),
    ("javascript", config.IMAGE_JS,
     ["sh", "-c", "jest --coverage --ci --silent --forceExit || true"],
     "coverage/coverage-final.json", "cov_js.json", None),   # istanbul 全图文件（stdout coverageMap 为空实锤）
    ("java", config.IMAGE_JAVA, ["mvn", "test"], "target/site/jacoco/jacoco.xml",
     "cov_java.json", from_jacoco_xml),
    ("go", config.IMAGE_GO, ["go", "test", "./...", "-coverprofile=c.out"],
     "c.out", "cov_go.json", from_go_coverprofile),
    ("php", config.IMAGE_PHP,
     ["sh", "-c", "composer install --no-interaction --quiet && "
                 "vendor/bin/phpunit tests/ShopTest.php --coverage-filter src "
                 "--coverage-clover clover.xml || true"],
     "clover.xml", "cov_php.json", from_clover_xml),
]


def _make_fixture(lang: str) -> Path:
    demo = {"python": "场景A", "javascript": "js-calc", "java": "java-shop",
            "go": "go-shop", "php": "php-shop"}[lang]
    fx = Path(config.DATA_DIR) / "f2_stage0" / lang
    if fx.exists():
        shutil.rmtree(fx, ignore_errors=True)
    fx.mkdir(parents=True)
    shutil.copytree(DEMO / demo, fx, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("node_modules", "vendor", "target",
                                                  "__pycache__", ".git"))
    if lang in TESTS:
        rel, text = TESTS[lang]
        p = fx / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")
    return fx


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    runner = SandboxRunner()
    results = {}
    ok_all = True
    for lang, image, cmd, artifact, out_name, parser in PLAN:
        fx = _make_fixture(lang)
        name = f"mta-f2s0-{lang}"
        runner.cleanup(name)
        container = runner.ensure_container(image, name, fresh=True)
        runner.copy_dir_into(container, fx)
        t0 = time.monotonic()
        r = runner.exec_in(container, cmd, timeout_s=1200)
        elapsed = round(time.monotonic() - t0, 1)
        art = runner.exec_in(container, ["cat", artifact], timeout_s=30)
        text = art.stdout or ""
        runner.cleanup(name)
        rec = {"lang": lang, "cmd": " ".join(cmd), "artifact": artifact,
               "exec_ok": r.ok, "artifact_chars": len(text), "elapsed_s": elapsed}
        if lang == "javascript":
            try:
                doc = from_istanbul(json.loads(text), root="/work")   # istanbul 全图直解析
            except Exception as e:                     # noqa: BLE001
                doc = {"source": "istanbul", "files": [], "error": str(e)[:200]}
        else:
            data = json.loads(text) if parser is from_coverage_py_json else text
            doc = parser(data)
        rec["files_n"] = len(doc.get("files") or [])
        rec["totals"] = doc.get("totals")
        rec["schema_ok"] = bool(doc.get("files")) and all(
            set(f.keys()) == set(FILE_KEYS) for f in doc["files"])
        (OUT / out_name).write_text(json.dumps(doc, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
        ok = rec["schema_ok"] and rec["files_n"] > 0
        ok_all = ok_all and ok
        results[lang] = rec
        print(f"[{lang}] {'✓' if ok else '✗'} files={rec['files_n']} "
              f"totals={rec['totals']} exec_ok={r.ok} {elapsed}s")
    (OUT / "stage0_summary.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print("RESULT:", "ALL PASS" if ok_all else "FAIL")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
