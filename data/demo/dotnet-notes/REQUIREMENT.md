# 验收需求（dotnet-notes，.NET Tier3 兜底·白包阶段5）

被测 NotesService.cs（无构建配置的散装 C#）：Add 添加备注并返回总数；Get 按索引取备注。
本阶段预期：走 Tier3 静态扫描（不执行、无覆盖率），报告明示未验证。
