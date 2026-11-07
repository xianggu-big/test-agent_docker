# mta-test:go122 —— Go 执行镜像（多语言工具链接入阶段3，白包）
# 官方 golang:1.22 基座：工具链完整（go test/build/vet 内建），零额外安装；
# 模块依赖随项目走（go.mod 在工作区，GOFLAGS=-mod=mod 容器内默认可解析 stdlib 零网络）。
FROM golang:1.22
WORKDIR /work
