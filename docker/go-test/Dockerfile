# mta-test:go126 —— Go 执行镜像（多语言工具链接入阶段3，白包；2026-11-08 升级）
# 官方 golang:1.26 基座：工具链完整（go test/build/vet 内建），零额外安装；
# 模块依赖随项目走（go.mod 在工作区，GOFLAGS=-mod=mod 容器内默认可解析 stdlib 零网络）。
#
# 升级原因（任务 678/679 实锤）：gin-master 的 go.mod 要求 `go 1.26.0`，而 1.22 镜像
# 的 GOTOOLCHAIN=local 拒绝自动下载 → `go test` 秒失败且无任何 JSON 事件
# （输出仅一行 "go.mod requires go >= 1.26.0"）→ evaluate 0 通过 0 失败 → repair_env
# 三轮原地打转熔断。1.26 基座同时兼容 go.mod 声明 1.22/1.23/1.24/1.25 的项目
# （Go 的向后兼容承诺：新版工具链可编译旧版声明模块）。
FROM golang:1.26
WORKDIR /work
