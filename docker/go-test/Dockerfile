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
# 任务卡-F2 阶段 1 #2/#4：静态分析工具钉版本进镜像（卡 §6 坑预判 1——版本漂移防线）
# GOPROXY 直连 proxy.golang.org 被墙（connection refused 实锤）→ 国内镜像 goproxy.cn
ENV GOPROXY=https://goproxy.cn,direct
RUN go install github.com/fzipp/gocyclo/cmd/gocyclo@v0.6.0     && go install honnef.co/go/tools/cmd/staticcheck@2025.1.1     && go install github.com/go-gremlins/gremlins/cmd/gremlins@v0.5.1
# 任务卡-F 阶段 1 #7（L2 契约轨接 Go，2026-11-10）：加装 python3+requests——L2 契约
# 用例是 Python requests 黑盒（打 127.0.0.1:port 的 Go 服务），单容器闭环零跨容器网络：
# go run 服务（127.0.0.1:8080）与 pytest 同容器共存。pytest/jsonschema 是断言执行依赖。
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pip \
    && rm -rf /var/lib/apt/lists/* \
    && pip3 install --break-system-packages --no-cache-dir requests jsonschema pytest
