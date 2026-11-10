# 答案键（对答题人/评测隐藏——本文件与 ground truth 同源）

| 缺陷 | 模块 | 位置 | 描述 |
|---|---|---|---|
| D1 | utils | text.py::clip | 上界差一：docstring 承诺闭区间 [lo, hi]，实现 `hi-1` 把上界排斥 |
| D2 | api_handler | routes.py::read_item | 缺失条目返回 200+null，违反 404 契约 |
| D3 | db_access | store.py::save_item | insert 后漏 commit，持久化契约违约 |
| D4 | web | index.html | 提交按钮请求进行中未禁用（双击重复提交） |
| D5 | config | config.py::MAX_ITEMS | 需求 100，实现 10（配置与需求不一致） |

对应模块画像预期：utils=pure_func+docstring→单元；api_handler=api_handler→契约+场景；
db_access=db_calls→单元+db_assert；web=page→UI 幂等；config=pure_data→跳过。
