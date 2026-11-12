"""多语言契约轨渲染（任务卡-F 阶段 1/2/3 收口，2026-11-12）：
JS supertest / Java HttpClient / PHP curl 三形态契约用例渲染。

设计（与 render_contract_module 同构——同一 endpoints/payload 数据源，只换代码生成）：
- 复用 cg.negative_variants/example_from_schema 的确定性数据（零 LLM 契约轨不变）；
- 语言形态差异只在 HTTP 客户端调用行与断言行（_emit 抽象分层已隔离）；
- **真机验收需额度**（LLM 需求断言层可选），渲染层+单测本轮零额度交付；
- schema 如实 None 的端点（go_route 等静态提取）：POST/PUT/PATCH 发空对象 JSON
  （contract_gen payload 兜底同语义）。

Java 形态：JUnit5 + java.net.http.HttpClient（JDK11+ 内置，零第三方依赖——
镜像零改动）；PHP 形态：PHPUnit + curl 扩展（php83 镜像自带）；
JS 形态：jest + supertest（需项目 devDependencies 或全局——真机跑时装）。
"""
import json
import re

from .gen_tests import _sanitize_name


# —————————————————————————— JS supertest ——————————————————————————

def _js_string(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def render_contract_js(endpoints: list[dict], base_url: str
                       ) -> tuple[str, list[dict], list[str]]:
    """JS supertest 契约用例（jest）。返回 (代码, 用例记录, 跳过路由)。
    依赖：npm i -D supertest jest（真机跑时装，渲染层只产代码）。"""
    lines = [
        "// AI 契约黑盒用例（L2 · JS supertest 形态）",
        'const request = require("supertest");',
        f"const BASE = {_js_string(base_url)};",
        "",
    ]
    cases: list[dict] = []
    skipped: list[str] = []
    used_names: set = set()
    for ep in endpoints:
        method = ep["method"].lower()
        path = ep["path"]
        has_body = ep["method"] in ("POST", "PUT", "PATCH")
        if any(p.get("in") == "path" and p.get("required") for p in ep.get("params") or []):
            skipped.append(f"{ep['method']} {path}")     # 路径参数端点：顺序场景轨负责
            continue
        success = ep.get("success_code", 200)
        fname = _sanitize_name(f"test_{method}_{path}_契约黑盒".replace("/", "_"),
                               len(cases), used_names)
        body = (f'describe("{ep["method"]} {path}", () => {{\n'
                f"  it('{fname.replace(chr(39), '')}', async () => {{\n")
        if has_body:
            payload = {}
            if isinstance(ep.get("request_schema"), dict):
                from .contract_gen import example_from_schema
                payload = example_from_schema(ep["request_schema"]) or {}
            body += f"    const payload = {_js_string(payload)};\n    "
            body += f"const res = await request(BASE).{method}(\"{path}\").send(payload);\n"
        else:
            body += f"    const res = await request(BASE).{method}(\"{path}\");\n"
        body += (f"    expect(res.status).toBe({success});   "
                 f"// 契约断言[接口契约]\n")
        if isinstance(ep.get("response_schema"), dict):
            # schema 校验留在断言注释形态（JS 侧 jsonschema 依赖视项目而装）——
            # 状态码契约是硬断言，schema 断言用字段抽查（首字段必须存在）
            props = list((ep["response_schema"].get("properties") or {}).keys())
            if props:
                body += (f'    expect(typeof res.body.{props[0]}).not.toBe("undefined");'
                         f"   // 契约断言[接口契约] 字段 {props[0]}\n")
        body += "  });\n});\n"
        lines.append(body)
        cases.append({"name": fname, "method": f"契约黑盒-{ep['method']}",
                      "expected": f"{success}（接口契约）",
                      "expected_basis": "接口契约", "route": f"{ep['method']} {path}"})
    return "\n".join(lines) + "\n", cases, skipped


# —————————————————————————— Java HttpClient（JDK11+，零第三方） ——————————————————————————

_JAVA_METHOD = {"GET": "GET", "POST": "POST", "PUT": "PUT", "PATCH": "PUT",
                "DELETE": "DELETE"}


def _java_string(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_contract_java(endpoints: list[dict], base_url: str, class_name: str = "TestContractApi"
                         ) -> tuple[str, list[dict], list[str]]:
    """Java 11 HttpClient 契约用例（JUnit5，零第三方 HTTP 依赖）。
    渲染成单类多 @Test（与 L1 Java 类壳渲染同构）。"""
    lines = [
        "import org.junit.jupiter.api.Test;",
        "import static org.junit.jupiter.api.Assertions.*;",
        "import java.net.URI;",
        "import java.net.http.*;",
        "",
        f"public class {class_name} {{",
        f"    static final String BASE = {_java_string(base_url)};",
        "    static final HttpClient CLIENT = HttpClient.newHttpClient();",
        "",
    ]
    cases: list[dict] = []
    skipped: list[str] = []
    used_names: set = set()
    for ep in endpoints:
        if any(p.get("in") == "path" and p.get("required") for p in ep.get("params") or []):
            skipped.append(f"{ep['method']} {path}" if False else f"{ep['method']} {ep['path']}")
            continue
        method = _JAVA_METHOD.get(ep["method"], "GET")
        success = ep.get("success_code", 200)
        fname = _sanitize_name("test_" + re.sub(r"[^A-Za-z0-9]", "_",
                                                f"{ep['method']}_{ep['path']}").lower()
                               + "_契约黑盒", len(cases), used_names)
        has_body = ep["method"] in ("POST", "PUT", "PATCH")
        body = f"    @Test\n    void {fname}() throws Exception {{\n"
        if has_body:
            payload = {}
            if isinstance(ep.get("request_schema"), dict):
                from .contract_gen import example_from_schema
                payload = example_from_schema(ep["request_schema"]) or {}
            body += f"        String payload = {_java_string(json.dumps(payload))};\n"
            body += (f"        HttpRequest req = HttpRequest.newBuilder(URI.create(BASE + "
                     f"{_java_string(ep['path'])}))\n"
                     f"                .header(\"Content-Type\", \"application/json\")\n"
                     f"                .method(\"{method}\", HttpRequest.BodyPublishers.ofString(payload))\n"
                     "                .build();\n")
        else:
            body += (f"        HttpRequest req = HttpRequest.newBuilder(URI.create(BASE + "
                     f"{_java_string(ep['path'])}))\n"
                     f"                .method(\"{method}\", HttpRequest.BodyPublishers.noBody())\n"
                     "                .build();\n")
        body += ("        HttpResponse<String> res = CLIENT.send(req, "
                 "HttpResponse.BodyHandlers.ofString());\n")
        body += (f"        assertEquals({success}, res.statusCode(), "
                 f"{_java_string('契约断言[接口契约] ' + ep['method'] + ' ' + ep['path'])});\n")
        body += "    }\n"
        lines.append(body)
        cases.append({"name": fname, "method": f"契约黑盒-{ep['method']}",
                      "expected": f"{success}（接口契约）",
                      "expected_basis": "接口契约", "route": f"{ep['method']} {ep['path']}"})
    lines.append("}\n")
    return "\n".join(lines) + "\n", cases, skipped


# —————————————————————————— PHP curl（PHPUnit，零第三方） ——————————————————————————

def render_contract_php(endpoints: list[dict], base_url: str, class_name: str = "ContractApiTest"
                        ) -> tuple[str, list[dict], list[str]]:
    """PHP curl 契约用例（PHPUnit 类壳；<?php 必须首行——php83 渲染纪律）。
    payload 用 PHP 数组字面量 ['k' => v]（JSON 对象花括号是 JS 语法=PHP Parse error）。"""

    def _php_array(payload: dict) -> str:
        # PHP 数组字面量（递归，标量 json_encode）
        if not isinstance(payload, dict):
            return json.dumps(payload, ensure_ascii=False)
        items = [f"{json.dumps(k, ensure_ascii=False)} => {_php_array(v)}"
                 for k, v in payload.items()]
        return "[" + ", ".join(items) + "]"

    lines = [
        "<?php",
        "use PHPUnit\\Framework\\TestCase;",
        "",
        f"final class {class_name} extends TestCase {{",
        f"    const BASE = {json.dumps(base_url)};",
        "",
        "    private function http(string $method, string $path, ?array $payload = null): array {",
        "        $ch = curl_init(self::BASE . $path);",
        "        $opts = [CURLOPT_RETURNTRANSFER => true, CURLOPT_CUSTOMREQUEST => $method,",
        "                 CURLOPT_TIMEOUT => 10];",
        "        if ($payload !== null) {",
        "            $opts[CURLOPT_POSTFIELDS] = json_encode($payload);",
        "            $opts[CURLOPT_HTTPHEADER] = ['Content-Type: application/json'];",
        "        }",
        "        curl_setopt_array($ch, $opts);",
        "        $body = curl_exec($ch);",
        "        $code = curl_getinfo($ch, CURLINFO_RESPONSE_CODE);",
        "        curl_close($ch);",
        "        return [$code, $body];",
        "    }",
        "",
    ]
    cases: list[dict] = []
    skipped: list[str] = []
    used_names: set = set()
    for ep in endpoints:
        if any(p.get("in") == "path" and p.get("required") for p in ep.get("params") or []):
            skipped.append(f"{ep['method']} {ep['path']}")
            continue
        success = ep.get("success_code", 200)
        fname = _sanitize_name("test_" + re.sub(r"[^A-Za-z0-9]", "_",
                                                f"{ep['method']}_{ep['path']}").lower()
                               + "_契约黑盒", len(cases), used_names)
        has_body = ep["method"] in ("POST", "PUT", "PATCH")
        body = f"    public function {fname}(): void {{\n"
        if has_body:
            payload = {}
            if isinstance(ep.get("request_schema"), dict):
                from .contract_gen import example_from_schema
                payload = example_from_schema(ep["request_schema"]) or {}
            body += (f"        [$code] = $this->http({json.dumps(ep['method'])}, "
                     f"{json.dumps(ep['path'])}, {_php_array(payload)});\n")
        else:
            body += f"        [$code] = $this->http({json.dumps(ep['method'])}, {json.dumps(ep['path'])});\n"
        body += (f"        $this->assertSame({success}, $code, "
                 f"{json.dumps('契约断言[接口契约] ' + ep['method'] + ' ' + ep['path'])});\n")
        body += "    }\n"
        lines.append(body)
        cases.append({"name": fname, "method": f"契约黑盒-{ep['method']}",
                      "expected": f"{success}（接口契约）",
                      "expected_basis": "接口契约", "route": f"{ep['method']} {ep['path']}"})
    lines.append("}\n")
    return "\n".join(lines) + "\n", cases, skipped
