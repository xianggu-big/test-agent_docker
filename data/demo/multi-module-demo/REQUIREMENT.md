# 多模块演示项目需求（multi-module-demo）

1. 文本工具（utils/text.py）：`clip(v, lo, hi)` 将 v 裁剪进闭区间 [lo, hi]（含两端）。
2. API（api_handler/routes.py）：`GET /items/{iid}` 查询条目，不存在必须返回 404；
   `POST /items` 创建条目。
3. 数据访问（db_access/store.py）：`save_item(item)` 写入并持久化（commit）；
   `rename_item(iid, name)` 更新名称并持久化。
4. 页面（web/index.html）：表单提交创建条目；提交按钮在请求进行中必须禁用防重复提交。
5. 配置（config.py）：`MAX_ITEMS` 需求规定为 100。

模块各自独立、边界清晰：utils 纯函数（有 docstring）、api_handler 路由、
db_access SQL、web 静态页面、config 纯常量。
