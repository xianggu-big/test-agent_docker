# mta-test:grpcurl —— gRPC 黑盒执行镜像（黑盒能力补齐阶段4）
# fullstorydev/grpcurl 官方镜像为极简基座（无 sh/sleep，长驻容器 exec 不可用——真机实锤），
# 故取其静态 grpcurl 二进制（v1.9.3）放入 alpine 基座（3.20 拉取病态慢 12 分钟未完成——改用本地已有 alpine:latest，二进制静态无 glibc 依赖）：sh/mkdir/sleep 在位，exec 编排可用。
# 二进制经 docker cp 从官方镜像提取（提取命令见任务卡验收记录），随构建上下文进镜像。
FROM alpine:latest
COPY grpcurl /bin/grpcurl
RUN chmod +x /bin/grpcurl
WORKDIR /work
