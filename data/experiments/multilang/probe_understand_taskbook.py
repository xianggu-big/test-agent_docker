# -*- coding: utf-8 -*-
"""understand 真实任务书验收探针（UTF-8 文件输入，避控制台编码坑）。"""
import sys, os, json
sys.path.insert(0, r"D:\test agent_docker")
from test_agent.skills.understand import understand_requirement

TASKBOOK = """你是资深 Go 软件测试工程师，负责对当前项目进行系统化测试。你的任务是独立理解项目、分析需求、设计测试用例、执行测试，并提交有证据支持的缺陷报告。你不能假设项目一定存在 Bug，也不能为了完成任务而编造缺陷。
一、项目理解：首先分析 go.mod 和 Go 版本要求、项目目录结构、项目启动入口、核心业务模块、API 路由和中间件、数据库模型与数据访问逻辑、已有测试代码。输出项目结构分析和测试范围。
二、黑盒测试：在不依赖源码内部实现细节的情况下设计测试。至少考虑：正常输入、异常输入、边界值、空值和缺失字段、非法参数、身份认证、权限控制、HTTP 状态码、响应数据正确性、分页和排序、错误处理、状态变化、多次重复请求。每个测试用例必须记录：用例编号、测试目标、前置条件、请求或输入数据、预期结果、实际结果、执行状态、证据。
三、白盒测试：分析源码中的关键函数和逻辑分支，重点检查条件判断、边界条件、nil 指针、错误返回值处理、资源释放、并发安全、Context 取消和超时。根据分析结果生成必要的 Go 测试代码。
四、执行测试：go version / go mod tidy / go build ./... / go test ./... / go test -race / go test -cover / go vet。如有必要生成 coverage.out 并分析覆盖率。你必须区分编译失败、测试断言失败、环境配置失败、外部依赖失败、程序运行异常。不能将环境问题直接报告为代码缺陷。不能伪造测试执行结果。
五、缺陷报告：发现疑似缺陷后必须提供 Bug 编号、缺陷类型、严重程度、文件路径和行号、触发条件、复现步骤、预期行为、实际行为、测试证据、修复建议。对于不能复现的问题标记为疑似问题。
六、约束：未经授权不修改原始项目代码，不修改原有测试来让测试通过，不连接生产数据库，不执行破坏性操作，不泄露项目密钥，所有测试尽量在隔离环境中执行，每个结论都必须有证据。
七、最终交付：项目理解报告、黑盒测试用例集、白盒分析报告、自动生成的 Go 测试代码、测试执行日志、缺陷报告、代码覆盖率报告、环境问题报告、测试总结。
被测项目是一个订单分级模块：GetTier(score) 按积分分级（>=90 返回 A / >=60 返回 B / 其余返回 C），Add(a,b) 返回两数之和。"""

out = understand_requirement(os.environ["DEEPSEEK_API_KEY"], TASKBOOK,
                             project_hint="Go 订单分级模块")
print(json.dumps(out, ensure_ascii=False, indent=2))
if out:
    banned = ("缺陷报告", "交付", "项目理解报告", "覆盖率报告", "测试总结", "执行日志")
    leaked = [g for g in out["test_goals"] if any(b in g for b in banned)]
    print("\n元指令泄漏检查（test_goals 中不得出现任务规范词）:", "PASS 无泄漏" if not leaked else f"FAIL {leaked}")
