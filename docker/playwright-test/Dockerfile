# mta-test:playwright —— UI 黑盒执行镜像（黑盒能力补齐阶段3）
# 基座=官方 playwright 镜像：浏览器（chromium/firefox/webkit）+系统依赖在 /ms-playwright，
# Python 侧工具链（playwright pip 包/pytest）随装——镜像只装工具链不装项目依赖。
# playwright pip 版本必须与基座浏览器版本严格对齐（1.49.1），否则报 Executable doesn't exist。
FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
RUN pip install --no-cache-dir -i ${PIP_INDEX_URL} playwright==1.49.1 pytest==8.3.4

WORKDIR /work
