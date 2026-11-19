# 接口

`POST /api/run`：JSON中的csv文本，省略使用样例；或multipart字段file。最大5MB。

`POST /api/m1`至`POST /api/m7`：独立运行到指定模块，JSON结构同运行接口。

`GET /api/history`：历史批次。`POST /api/review`接收id、revision和decision（通过或退回）。`GET /api/evidence.csv`提供单一证据导出。

结构错误422，版本变化409，文件过大413；错误返回code与message。所有页面可从左侧七个模块入口访问。

`GET /api/runs/<id>`读取指定批次；页面与证据CSV通过run_id查询参数绑定批次，模块导航保留该参数。

## 调拨收发对账（运行期，独立账本）

`POST /api/recon/events`：JSON `{batch_id?, events:[...]}`，事件类型与守恒口径见 docs/RECONCILIATION.md。省略 batch_id 时由事件内容派生幂等批次；同批重跑返回 reused 且不新增加扣减。

`POST /api/recon/demo`：装载/重跑内置演示批次。`GET /api/recon/batches` 列批次；`GET /api/recon/batches/<id>` 取当前对账结论；`GET /api/recon/batches/<id>/ledger` 取只增事件凭证与版本历史。看板页面 `/recon`。任一条事件非法整批回滚（422，`RECON_*` 原因码），上一版结论保留。

JSON请求体必须是对象，数组、标量、null、损坏JSON及错误上传字段返回400，且不运行默认样例。
