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
| 领货/交割 | `/events/{id}/pickups/*`、`/deliveries`、`/receipts` | 赢家/承运人/司机/卖家 | 详见下文「成交后现场领货 / 交割」 |

错误统一返回：

```json
{"error": {"code": "budget_exceeded", "message": "..."}}
```

常用错误码：`budget_required`、`budget_exceeded`、`budget_below_commitment`、`below_reserve`、`must_raise`、`lot_closed`、`event_closed`、`reserve_locked`、`forbidden`、`invalid_token`。

## 成交后现场领货 / 交割（Pickup & Delivery）

只适用于**已成交**（结标且有赢家）的标的；**整场活动关闭后仍可委托、派司机、交货与查回执**。交割流程不修改赢家、成交价或预算，也不接入任何支付或外部物流。

### 角色与“当前可实际领货人”

受托方就是同场已注册的账号（可用既有 `buyer/seller` 账号，也可注册 `carrier/driver` 角色）。对某件标的，同一时刻只有一个可实际领货的人：

1. 有有效司机 → **司机**；
2. 否则有承运人 → **承运人**；
3. 都没有 → **赢家本人**。

委托/指派目标必须是同活动已有账号，且对该件不能是：该件卖家、该件赢家、操作人自己。

- 买家（赢家）对自己**尚未交付**的拍品批量**委托 / 改派 / 收回**承运人。重新委托（即使还是同一承运人）会清空旧司机并作废旧凭据；只影响所选拍品。
- 承运人对自己**当前受托且未交付**的拍品批量**指派 / 更换 / 撤回**司机。
- **收回必须存在当前承运人委托；撤回司机必须存在当前司机指派**。批量中任一件不满足（不存在、非本人权利、未成交、已交付、缺少对应委托等），**整批不变**（单事务校验后写入）。
- 已交付拍品不能再委托、改派、收回或更换/撤回司机。

### 核验凭据

每件未交付拍品的“当前授权”对应一枚稳定的不透明凭据 `pck_...`（HMAC 绑定 活动/拍品/授权版本/当前领货人，密钥持久化在 `meta` 表，重启不变）。赢家、该件卖家、当前承运人/司机登录后可在状态响应中直接取得，也可单独调凭据接口。

任何授权变更都会把该件 `auth_version + 1`，因此**旧凭据立即失效——即使新授权恰好是同一个领货人**；其他未变更拍品的凭据不受影响。交付后不再签发凭据。

### 卖家一次交货 + 幂等请求号

卖家交货时提交：**领货账号**（`id`/数字 id/用户名均可）、**一组互不重复的拍品**、**逐件凭据**、以及该次交付的 **请求号 `request_id`**（也可用 `Idempotency-Key` 请求头）。

- 所有拍品在同一事务内校验：均属于该卖家、已成交、未交付、凭据为当前授权有效凭据、且当前授权领货人都是所提交的同一账号。任一件过期/已交付/不属于该卖家/领货人不符 → **整组拒绝，不写任何状态**。
- 通过后生成**一份回执**（一个回执号，含全部拍品与每件“确认时”的赢家/承运人/司机/实际领货人快照及交付时间）。
- 同一 `(活动, 卖家, request_id)` 重送**相同内容**（拍品顺序不限，按拍品 id 规范化哈希）→ 返回**原回执**（资源型路由首次 `201`、重放 `200`，带 `replayed:true`）。
- 同一 `request_id` 但内容不同 → `409 idempotency_conflict`。不同卖家可复用同一请求号。
- 交付与改派/收回竞争时，所有操作都在进程锁 + `BEGIN IMMEDIATE` 事务内串行：变更先提交则旧凭据失效导致交货失败；交货先提交则后续变更以 `already_delivered` 拒绝。不会出现按变更前后两套授权同时交货。

### 回执可见性

- **卖家**与**实际领货人**（实际到现场的司机，或未派司机时的承运人/赢家）可见整份回执与全部明细。
- **对应赢家**可见回执，但同一回执含多个赢家时，**只返回该赢家自己拍品的明细、`lot_ids` 与委托快照**。
- 其他人（含未涉及的买家）对**已存在**的回执得到 `403 forbidden`；回执号不存在或不属于该活动才是 `404`。

### 主要接口（规范路径）

| 方法 | 路径 | 身份 | 说明 |
| --- | --- | --- | --- |
| GET | `/events/{id}/lots/{lot}/pickup` | 赢家/卖家/当前承运人或司机 | 交割状态、当前授权、（未交付）凭据 |
| GET/POST | `/events/{id}/lots/{lot}/credential` | 同上 | 取得当前授权凭据 |
| POST | `/events/{id}/pickups/delegate` | 赢家 | 批量委托/改派承运人 `{"lot_ids":[...],"carrier_id":..}` |
| POST | `/events/{id}/pickups/revoke` | 赢家 | 批量收回（须均有当前委托） |
| POST | `/events/{id}/pickups/assign-driver` | 当前承运人 | 批量指派/更换司机 `{"lot_ids":[...],"driver_id":..}` |
| POST | `/events/{id}/pickups/revoke-driver` | 当前承运人 | 批量撤回司机（须均有司机） |
| POST | `/events/{id}/deliveries` | 该批拍品卖家 | 交货确认（资源型，首次 201/重放 200） |
| POST | `/events/{id}/deliver` | 该批拍品卖家 | 同上（动作型，成功/重放均 200） |
| GET | `/events/{id}/receipts` | 任意相关人 | 自己可见的回执列表（赢家视图自动裁剪） |
| GET | `/events/{id}/receipts/{rid}` | 卖家/赢家/实际领货人 | 单份回执（赢家只看自己明细） |
| GET | `/events/{id}/lots/{lot}/receipt` | 同上 | 按拍品查回执 |
| GET | `/events/{id}/pickups` | 任意登录用户 | 与我相关的领货状态（`?as=winner|carrier|driver|seller&status=pending|delivered`） |

交货请求示例：

```json
{
  "request_id": "seller-a-20261127-01",
  "picker_id": 7,
  "items": [
    {"lot_id": 11, "credential": "pck_...."},
    {"lot_id": 12, "credential": "pck_...."}
  ]
}
```

也兼容扁平形式 `{"request_id":"...","picker":"driver_name","lot_ids":[11,12],"credentials":["...","..."]}`。

为适配不同命名习惯，同一操作还挂载了大量别名（均经过同样的严格规则）：`/pickups/reassign|recover|withdraw`、`/delegations[...]`、`/driver-assignments[...]`、`/carrier/(assign|replace|revoke|withdraw)-driver`、`/deliveries`、`/handovers`、`/receipts`、`/vouchers`、`/lots/{lot}/(delivery|handover|authorization|...)`、单件 `POST /lots/{lot}/delegate|assign-driver|deliver`、以及 RPC 风格 `POST /events/{id}/pickup`，body 带 `"action":"delegate|revoke|assign_driver|revoke_driver|deliver"`。账号字段兼容 `carrier(_id|_account|_user)?`、`trustee(_id)?`、`driver(_id|_account)?`、`picker(_id)?`、`pickup_account`、`receiver`、`account`、`username` 等；凭据字段兼容 `credential|code|pickup_code|verification_code|token` 等。

### 隐私与持久化

- 公开接口 `GET /events/{id}`、`/lots`、`/lots/{lot}` 的响应结构**未加入**任何交割字段，不泄露凭据、承运人或司机身份。
- 新表 `pickup_auth / deliveries / delivery_items` 与拍卖数据在同一个 SQLite 文件；`delivery_items.lot_id UNIQUE` 是防重复交付的硬约束。重启后授权、回执、凭据（同一 HMAC 密钥）均保留。旧库的 `users` 角色 CHECK 会在启动时无损迁移以支持 `carrier/driver` 角色。

## 线下成交款结算（Offline Settlement）

活动管理员按银行回单登记**收款**与**退票**；卖家只能对**同时存在有效收款与已确认交割回执**的拍品申请打款。服务只记账，不连接任何银行或支付平台；**活动关闭后仍可办理**。本功能不修改赢家、成交价、预算或已有交割回执。

### 记账规则

- **收款登记**（管理员）：`流水号 + 付款买家 + 金额 + 一组互不重复拍品`，可跨多个卖家。所有拍品必须：本场被该买家赢得、当前没有有效收款；金额必须等于这些拍品 `purchase_price` 之和。任一条不满足则**整组不变**（单事务）。
- **流水号幂等**：同一 `(活动, 流水号)` 重报相同内容（拍品顺序无关，按排序 id 规范化哈希）返回原记录（资源路由首次 `201`、重放 `200` 且 `replayed:true`）；内容不同返回 `409 idempotency_conflict`。
- **退票**：管理员按流水号整笔退票。只停用**该流水自己的**收款分配，因此重复退票绝不会撤掉后来用**新流水**做的补缴；重复退票幂等返回原退票记录。
- **可结算收入**：每件拍品在「存在 `active=1` 的收款分配」且「存在 `delivery_items` 交割记录」时，按成交价计入其卖家收入；两个条件谁先成立都行（收入为状态推导，天然不重复计）。
- **打款申请**：卖家以 `request_id`（同活动+卖家唯一）申请**正整数**金额；`可申请额度 = max(0, 收入 − 已打款 − 待打款占用)`。同 `request_id` 重送同金额返回原申请，改金额 `409`。
- **确认 / 释放**：管理员可把待打款申请置为 `paid`（已打款）或 `released`（释放占用）；同动作重复执行幂等且不重复记账；对已取消/已释放的申请确认会被 `409` 拒绝。
- **退票重平衡**：退票撤掉对应收入但不撤销已打款。若 `收入 − 已打款` 不足以覆盖待打款，则按申请 id（成立先后）**从最新起整单取消**，直到剩余待打款被覆盖；当已打款 > 剩余收入时取消全部待打款，`待抵扣差额 = max(0, 已打款 − 收入)`，由该卖家日后新增收入自动先抵扣（额度公式 `max(0, 收入 − 已打款 − 待打款)` 已内含这一优先抵扣）。一笔退票跨多卖家时，各卖家的重平衡在**同一事务**一起生效。
- **并发**：收款、交割、退票、打款、确认/释放都在同一把进程内 `RLock` + SQLite `BEGIN IMMEDIATE` 事务内串行；`payment_items(lot_id) WHERE active=1` 部分唯一索引与 `payouts(活动,卖家,request_key)` 唯一约束是跨进程硬保护。结果等价于某个实际生效顺序。
- **隐私**：收款/退票/全量打款记录仅管理员；卖家只能看自己的额度与申请状态、或按 id/请求号查自己的申请；其他人（含买家、其他卖家）返回 `403/404`，未认证 `401`。公开拍卖接口响应不含任何结算字段。所有新表与拍卖数据同库，重启后记录与额度（全部由持久状态推导）保持一致。

### 主要接口（规范路径，另挂载大量命名别名）

| 方法 | 路径 | 身份 | 说明 |
| --- | --- | --- | --- |
| POST | `/events/{id}/payments` | 管理员 | 登记收款（资源型，首 `201`/重放 `200`） |
| GET | `/events/{id}/payments` | 管理员 | 本场收款记录（`?status=valid\|refunded`） |
| GET | `/events/{id}/payments/{serial_or_pk}` | 管理员 | 单笔收款 |
| POST | `/events/{id}/refunds` | 管理员 | 按 `serial_no` 退票（首 `201`/重退 `200`） |
| POST | `/events/{id}/payments/{serial}/refund` | 管理员 | 动作型退票（始终 `200`） |
| GET | `/events/{id}/refunds` | 管理员 | 退票记录 |
| POST | `/events/{id}/payouts` | 卖家 | 申请打款（首 `201`/重放 `200`） |
| GET | `/events/{id}/payouts` | 卖家(自己)/管理员(全部) | 打款申请与状态，支持 `?status=&request_id=&seller_id=` |
| GET/PATCH | `/events/{id}/payouts/{pk_or_request_id}` | 卖家本人/管理员 | 申请详情；PATCH `{"status":"paid"\|"released"\|"cancelled"}` |
| POST | `/events/{id}/payouts/{pid}/confirm` | 管理员 | 确认已打款（重复确认幂等） |
| POST | `/events/{id}/payouts/{pid}/release` | 管理员 | 释放待打款占用（重复释放幂等） |
| POST | `/events/{id}/payouts/{pid}/cancel` | 管理员 | 取消申请（释放占用） |
| GET | `/events/{id}/settlement` | 卖家 | 自己的额度视图（含 `available_amount/pending_amount/paid_amount/deficit_amount/settled_income` 与 `payouts`） |
| GET | `/events/{id}/sellers/{seller_id}/settlement` | 管理员 | 指定卖家额度 |
| GET | `/events/{id}/settlement/records` | 管理员 | 收款/退票/打款三合一核对 |

收款请求示例：

```json
{
  "serial_no": "ICBC20261127001",
  "buyer_id": 7,
  "amount": 1230,
  "lot_ids": [11, 12, 13]
}
```

打款申请示例：`{"request_id":"seller-a-po-0001","amount":500}`（也支持 `Idempotency-Key` 头）。

为适配不同命名习惯，同一能力还挂载在 `/bank-receipts`、`/bank-payments`、`/remittances`、`/refund`、`/bounces`、`/chargebacks`、`/withdrawals`、`/withdraw`、`/cash-out(s)`、`/payout-requests`、`/my-payouts`、`/balance`、`/earnings`、`/income`、`/wallet`、`/my-settlement`、`/settlements/me`、`/settleable-lots`、`/admin/*` 以及拼音 `/shoukuan|tuipiao|dakuan|jiesuan` 等路径；字段兼容 `bank_serial(_no)`、`transaction_no`、`payer_id`、`reference(_no)`、`request_no` 等大量别名，并兼容 camelCase / 中划线 / 下划线与中文字段名。

## 代码结构

```
app/
  domain.py        纯规则：领先者、当前价、预算占用（无 DB / 无 HTTP）
  db.py            SQLite schema、连接、串行化写事务、角色约束迁移
  service.py       业务编排（校验、状态迁移、审计日志），继承 PickupMixin / SettlementMixin
  pickups.py       领货/交割规则、凭据 HMAC、幂等哈希、回执与批量事务
  pickup_routes.py 领货/交割 HTTP 路由 + 多命名别名与宽松字段解析
  settlements.py   线下收款/退票/可结算收入/打款额度与退票重平衡规则（SettlementMixin）
  settlement_routes.py 结算 HTTP 路由 + 大量路径/字段别名与关键词兜底
  main.py          FastAPI 路由工厂 create_app()
  models.py        Pydantic 请求/响应模型（金额字段 strict int）
  errors.py        ApiError → {"error": {...}}
tests/
  test_domain.py              规则单元测试
  test_api.py                 端到端 API
  test_edges.py               边界与隐私
  test_concurrency.py         线程级并发 / 双连接同文件
  test_http_concurrency.py    真实 uvicorn 并行请求
  test_pickup_delivery.py     领货/承运人/司机/交货/回执/重启/并发
  test_pickup_http_concurrency.py  交货 vs 改派的真实 HTTP 竞争
```

## 已知取舍

- 认证用一次性发放的 Bearer token，存 SHA-256 哈希；未做 token 刷新/吊销。
- 结标后保留所有出价记录（历史与定价依据），但结标后不再参与预算计算。
- 同一活动内用户名字唯一，用于绑定身份展示；跨活动通过 `event_id` 隔离。
