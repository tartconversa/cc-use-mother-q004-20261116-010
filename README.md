# LoopLake 回收物料数据台

这是面向社区回收运营分析员的本地数据湖与数仓服务。它将智能回收箱投递、地磅入出站、转运和兑付事件保存为不可改写的原始记录，并生成带有效期维度、物料对账、付款校验和冻结发布的街道月度视图。所有示例均为合成数据。

## 使用

运行环境为 Python 3.11+；依赖版本见 `requirements.txt`。测试：`python -m unittest discover -s tests -v`；语法检查：`python -m compileall -q looplake`；完整本地演示：`python -m looplake.demo`。HTTP 服务可运行 `uvicorn looplake.api:app`，并在 `/docs` 查阅接口；CLI 为 `python -m looplake.cli --help`。设置 `LOOPLAKE_DB` 可改变 API SQLite 路径。

先登记来源和带有效期的街道、社区、设备、物料、站点与人员维度，再按来源导入事件；同一来源和外部 ID 的不同正文会拒绝，完全重复事件幂等返回。维度未就绪的输入进入隔离状态，登记维度后可按 ingestion ID 重试。批量导入支持清单指纹和检查点，防止使用不同清单误续跑。

同一物料批次的投递与地磅入站重量需在容差内相符；不同物料、出站超入站、付款与费率不符均产生可解释的差异。街道月度报表仅纳入已解决批次，不会将投递、地磅与转运三条记录相加。发布前读取预览指纹；发布时再次校验数据与不可变名称。冻结版保留聚合、对账和事实血缘，日后的新批次不会修改旧版。

## 分类版本与口径

管理部门调整品类（如把玻璃拆成透明玻璃与杂色玻璃）时，不改写物料维度，而是登记可审核的分类版本：`POST /classifications` 创建版本及类目，`PUT /classifications/{scheme}/{version}/mappings` 登记带生效区间与依据的物料→类目映射。映射与逐事实裁定都是只追加的修订链：修订产生新 revision，撤回保留历史，任何时刻的归类依据均可经 `GET /facts/{id}/classification` 与行级血缘追溯。

同一物料在同一分类版本下命中零条或多条映射时，历史事实保持待判（unmapped/ambiguous），绝不猜测归类；有证据时可 `PUT .../decisions/{fact_id}` 逐事实裁定并记录依据，裁定可修订或撤回。月度与街道跨期报表（`GET /months/{month}/mart`、`GET /streets/{id}/mart`）默认当时口径（按事实物料），传 `scheme` 与 `scheme_version` 则按该可比口径聚合：行内携带 mapping_refs，待判事实单列并附候选依据。预览与发布同样接受口径参数；冻结版记录口径、映射依据与待判清单，映射修订或撤回会使旧预览指纹失效，已撤回的分类版本禁止再发布，但旧冻结版内容不变、可比较。

## 输入约定

时间带时区，重量用 kg，金额用元；业务日期由事件时间换算为 UTC 后记录。维度的 valid_to 为开区间。来源 channel 与事件 channel 必须一致。演示代码 `looplake/demo.py` 展示完整输入和可观察输出。HTTP 错误返回 `code` 和 `detail`；422 表示输入形状不合法，409 表示版本或业务冲突。
