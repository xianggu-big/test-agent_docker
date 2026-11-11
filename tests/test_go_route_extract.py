# -*- coding: utf-8 -*-
"""Go 路由提取（任务卡-F2 阶段 1 #6：D4 spec 链 Go 分支）单元测试。

口径锁：gin/echo/chi/net-http 四形态路由注册行提取；`:id` → `{id}` 归一化；
非路由行（注释/非 HTTP 方法调用）不匹配；schema 如实 None（静态提取无契约体，
诚实）；probe_endpoint_source 降级链挂载（spec→proto→go 路由）。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from test_agent.skills.endpoint_extract import parse_go_routes, probe_endpoint_source

_GIN = '''
package main

import "github.com/gin-gonic/gin"

func setup(r *gin.Engine) {
    r.GET("/rooms/:rid", readRoom)
    r.POST("/bookings", createBooking)
    r.DELETE("/bookings/:bid", cancel)   // 注释里不应重复计
}

func notARoute(r *gin.Engine) {
    r.MaxMultipartMemory = 8             // 非 HTTP 方法调用
    // r.GET("/commented", h)            // 注释行不提取
}
'''


def test_gin形态_提取与归一化(tmp_path):
    eps = parse_go_routes(_GIN)
    got = {(e["method"], e["path"]) for e in eps}
    assert ("GET", "/rooms/{rid}") in got            # :rid → {rid}
    assert ("POST", "/bookings") in got
    assert ("DELETE", "/bookings/{bid}") in got
    assert len(eps) == 3                             # 注释行/非方法调用不误收
    e_room = next(e for e in eps if e["path"] == "/rooms/{rid}")
    assert e_room["source"] == "go_route"
    assert e_room["request_schema"] is None and e_room["response_schema"] is None
    assert e_room["success_code"] == 200
    assert any(p["name"] == "rid" and p["in"] == "path" for p in e_room["params"])


def test_多框架形态_echo_chi_nethttp(tmp_path):
    text = (
        'package main\n'
        'func setup(e *echo.Echo) {\n'
        '    e.GET("/echo", h)\n'
        '}\n'
        'func chi(r *chi.Mux) {\n'
        '    r.Post("/chi", h)\n'
        '}\n'
        'func nh(mux *http.ServeMux) {\n'
        '    http.HandleFunc("/nh", handler)\n'
        '}\n'
    )
    got = {(e["method"], e["path"]) for e in parse_go_routes(text)}
    assert ("GET", "/echo") in got
    assert ("POST", "/chi") in got
    assert ("GET", "/nh") in got


def test_probe降级链_go分支(tmp_path):
    (tmp_path / "main.go").write_text(
        'package main\nimport "github.com/gin-gonic/gin"\n'
        'func s(r *gin.Engine) { r.GET("/ping", ping) }\n', encoding="utf-8")
    eps, how = probe_endpoint_source(tmp_path)
    assert how == "go_route" and eps and eps[0]["path"] == "/ping"


def test_probe_无go文件_不误入go分支(tmp_path):
    (tmp_path / "app.py").write_text("r.GET('/x', h)\n", encoding="utf-8")
    eps, how = probe_endpoint_source(tmp_path)
    assert eps is None and how == "no_spec"
