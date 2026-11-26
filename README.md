# 收藏品线上拍卖

使用 Python + FastAPI + SQLite 实现的密封最高愿付金额（proxy bid）拍卖服务。金额全部为整数；不接在线付款，成交只记账。

## 运行

```bash
python3 -m pip install -r requirements.txt
AUCTION_DB=./auction.db AUCTION_ADMIN_KEY=change-me \
  python3 -m uvicorn app.main:app --reload
```

- 交互式文档：`http://127.0.0.1:8000/docs`
- 健康检查：`GET /healthz`
- 环境变量：`AUCTION_DB`（SQLite 文件路径）、`AUCTION_ADMIN_KEY`（创建管理员的口令）

测试：

```bash
python3 -m pytest -q
```

## 角色与流程

| 角色   | 能做什么 |
| ------ | -------- |
| 买家   | 设定本场活动总预算、对标的提交/调高最高愿付金额、查看自己的已用/领先预留/剩余额度 |
| 卖家   | 创建、编辑自己的标的（底价），关闭自己的标的 |
| 管理员 | 创建活动（`POST /events` 公开）、创建管理员账号、关闭单件标的或整场活动 |

典型流程：

1. `POST /events` 创建活动 → 得到 `event_id`
2. `POST /events/{id}/admin`（携带 `admin_key`）创建管理员
3. `POST /events/{id}/users` 注册买家和卖家，响应里一次性返回 `token`
4. 卖家 `POST /events/{id}/lots` 建标的，设定整数底价
5. 买家 `PUT /events/{id}/budget` 设预算，然后 `POST /events/{id}/lots/{lot}/bids` 出价
6. 卖家/管理员 `POST /events/{id}/lots/{lot}/close` 结标；管理员 `POST /events/{id}/close` 结整场

所有需要身份的接口使用 `Authorization: Bearer <token>`。

## 拍卖规则（实现说明）

- **领先者**：最高上限者领先；上限相同则**首次有效出价较早**者领先（买家只能调高，所以同一买家在单件标的上只有一条记录，行 ID 就是首次出价顺序）。
- **当前价**：`max(底价, 第二高上限 + 1)`，且不超过领先者上限；只有一名竞拍者时即底价；无人出价时无价格、结标流拍。
- **底价校验**：低于底价的上限无效；出价只允许调高。
- **预算占用**：`已用 = 已结标且我赢下的成交价之和`；`领先预留 = 我当前正领先的每件未结标的上限之和`；落后者在该件不占额度，但其出价仍保留用于定价。
- **接受条件**：新报价必须先应用到该件，再算所有买家的 `已用 + 领先预留`，只要任一买家超出其活动预算就整体拒绝（包含报价者本人）。
- **并发**：报价与结标都在同一把进程内锁 + SQLite `BEGIN IMMEDIATE` 写事务中完成；结标按事务内当时有效的价格成交。因此同一买家的并发报价不会各自通过后合计超额，闭标与报价竞争时只会出现“报价先落地并影响价格”或“报价撞上已闭标”两种结果。
  （多进程部署时 SQLite 写锁同样串行化；单进程 uvicorn 无需额外配置。）
- **隐私**：公开端只暴露当前价、匿名领先者别名（`bidder-xxxxxx`，按活动内 HMAC 生成，稳定且不可反推）和卖家底价；不暴露任何最高上限、买家身份或活动总预算。买家仅能看到自己的预算与出价。

## API 摘要

| 方法 | 路径 | 身份 | 说明 |
| ---- | ---- | ---- | ---- |
| POST | `/events` | 公开 | 创建活动 |
| GET  | `/events/{id}` | 公开 | 活动状态 |
| POST | `/events/{id}/admin` | 公开+口令 | 创建管理员 |
| POST | `/events/{id}/users` | 公开 | 注册买家/卖家 |
| GET  | `/events/{id}/lots` | 公开 | 标的列表（公开视图） |
| GET  | `/events/{id}/lots/{lot}` | 公开 | 单件标的（公开视图） |
| POST | `/events/{id}/lots` | 卖家 | 建标的 |
| PATCH| `/events/{id}/lots/{lot}` | 卖家 | 编辑标的（有出价后底价锁定） |
| POST | `/events/{id}/lots/{lot}/close` | 卖家(本人)/管理员 | 结标 |
| POST | `/events/{id}/lots/{lot}/bids` | 买家 | 提交/调高上限 |
| GET  | `/events/{id}/budget` | 买家 | 自己的已用/预留/剩余 |
| PUT  | `/events/{id}/budget` | 买家 | 设定预算（不得低于已占用） |
| GET  | `/events/{id}/my-bids` | 买家 | 自己的出价与成交情况 |
| POST | `/events/{id}/close` | 管理员 | 结整场（逐件结标） |
| GET  | `/me` | 任意登录用户 | 当前身份 |

错误统一返回：

```json
{"error": {"code": "budget_exceeded", "message": "..."}}
```

常用错误码：`budget_required`、`budget_exceeded`、`budget_below_commitment`、`below_reserve`、`must_raise`、`lot_closed`、`event_closed`、`reserve_locked`、`forbidden`、`invalid_token`。

## 代码结构

```
app/
  domain.py   纯规则：领先者、当前价、预算占用（无 DB / 无 HTTP）
  db.py       SQLite schema、连接、串行化写事务
  service.py  业务编排（校验、状态迁移、审计日志）
  main.py     FastAPI 路由工厂 create_app()
  models.py   Pydantic 请求/响应模型（金额字段 strict int）
  errors.py   ApiError → {"error": {...}}
tests/
  test_domain.py            规则单元测试
  test_api.py               端到端 API
  test_edges.py             边界与隐私
  test_concurrency.py       线程级并发 / 双连接同文件
  test_http_concurrency.py  真实 uvicorn 并行请求
```

## 已知取舍

- 认证用一次性发放的 Bearer token，存 SHA-256 哈希；未做 token 刷新/吊销。
- 结标后保留所有出价记录（历史与定价依据），但结标后不再参与预算计算。
- 同一活动内用户名字唯一，用于绑定身份展示；跨活动通过 `event_id` 隔离。
