# test-agent 执行镜像 v1 —— Java 深度支持（Maven + JDK17 + JUnit5）
#
# 设计约定（与 python-test 镜像一致）：
#   ① pom.xml 依赖不进镜像 —— 「依赖检查 Skill」在容器内跑 mvn -q dependency:go-offline
#      预热后执行测试；镜像只固定工具链版本，保证可复现。
#   ② JVM 默认感知 cgroup 限额（UseContainerSupport），MaxRAMPercentage=75 防止
#      Maven 测试进程顶爆容器内存 —— 直接服务于 L4 的资源限额实验。
#   ③ 非 root / /work / sleep infinity / docker exec 驱动，同 mta-sandbox 模式。
#
# 构建：docker build -t mta-test:jdk17-maven .
# 国内网络可选：docker build --build-arg MAVEN_MIRROR=https://maven.aliyun.com/repository/public -t mta-test:jdk17-maven .
FROM maven:3.9-eclipse-temurin-17

# 构建期把镜像源写进 tester 用户的 settings.xml（运行时 mvn 同样生效）
ARG MAVEN_MIRROR=https://repo.maven.apache.org/maven2

ENV MAVEN_OPTS="-XX:+UseContainerSupport -XX:MaxRAMPercentage=75.0" \
    MAVEN_CONFIG=/home/tester/.m2

WORKDIR /work
RUN useradd --create-home --shell /bin/bash tester \
 && chown -R tester:tester /work \
 && mkdir -p /home/tester/.m2 \
 && printf '<settings>\n  <mirrors>\n    <mirror>\n      <id>central-mirror</id>\n      <url>%s</url>\n      <mirrorOf>central</mirrorOf>\n    </mirror>\n  </mirrors>\n</settings>\n' "$MAVEN_MIRROR" \
      > /home/tester/.m2/settings.xml \
 && chown -R tester:tester /home/tester/.m2
USER tester

# 自检（fail-fast）：JDK 与 Maven 版本可见即认为工具链可用
RUN java -version 2>&1 | head -1 \
 && mvn -version | head -2

CMD ["sleep", "infinity"]

# 任务卡-F 能力补齐（2026-11-12）：L2 契约用例=Python requests 黑盒（打 127.0.0.1 的
# Java 服务）——单容器闭环（与 go126 同策略）：python3+requests 同容器共存
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pip \
    && rm -rf /var/lib/apt/lists/* \
    && pip3 install --break-system-packages --no-cache-dir requests jsonschema pytest
