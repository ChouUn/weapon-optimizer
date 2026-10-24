# 枪械 Build 优化器

> Parent: 无
> Audience: user — 使用产物的人或 agent
> Genre: manual — 使用者怎么用产物
> Visibility: local — 仅供本机使用，可依赖本机环境与配置
> Maintain: living — 持续更新，保持内容有效

`spt-build` 是供 Agent 调用的只读命令行工具。
给定一把枪的精确模板 ID，它在 SPT 运行时物品数据中搜索完整配件树（虚拟 build），求后坐力最低、人机工效最高或二者加权最优的方案，可附加弹匣容量下限和下挂（枪挂榴弹发射器）硬条件。
每次调用向 stdout 输出一行 JSON，每个解都带最优性证明状态。
工具不读写存档、不改模组或服务器数据、不启停服务器，也不在游戏里装配或试射。

## 准备环境

- 安装 [uv](https://docs.astral.sh/uv/)，Python 3.12 由 uv 按需获取。
- 读取运行时数据时，本机 SPT 服务器需已在 `https://127.0.0.1:6969` 运行。

以下命令均在 `labs/weapon-optimizer/` 下执行：

```sh
uv sync --locked --python 3.12
uv run python -m unittest discover -s tests -v
```

第一条按 `uv.lock` 安装依赖（OR-Tools 9.15.6755、simplejson 4.2.0），第二条运行回归测试。

## 版本控制

本目录是独立 Git 仓库的根目录，外层游戏安装与存档不在该仓库范围内。

|配置|社区规则|本项目规则|
|---|---|---|
|[.gitignore](.gitignore)|保留 GitHub 的 Python 模板，来源链接见文件头|忽略本机环境、临时产物和指定路径的运行时快照，保留 `uv.lock` 与六份 JSON 示例|
|[.gitattributes](.gitattributes)|自动识别文本／二进制，使用内置 Python diff driver|文本在仓库与工作区统一为 LF，不转换二进制内容|
|[.editorconfig](.editorconfig)|项目边界、UTF-8、LF、末尾换行及通用空白规则|Python／TOML 四空格、JSON 两空格，保留 Markdown 行尾空格|

应提交 `uv.lock` 与 `examples/*.json`；完整运行时快照使用被忽略的 `runtime*.json`／`*snapshot*.json` 文件名，或放入 `snapshots/`。

## 跑通第一个例子

1. 把运行时数据存为快照：

   ```sh
   uv run spt-build snapshot --server https://127.0.0.1:6969 --insecure-loopback --session SESSION_ID --output runtime.json
   ```

   - 没有已保存的 schema 2 完整快照时，需在获准读取服务端后执行上方命令并保留输出。
   - `examples/*.json` 只是求解结果，不包含 `--snapshot` 所需的完整数据。
   - `SESSION_ID` 换成存档 ID，即 `SPT_Runtime/user/profiles/` 下存档文件名去掉 `.json`，必须是 24 位十六进制字符串。工具在任何 HTTP 请求前拒绝非法值，不使用默认或猜测的会话；有效值作为 `PHPSESSID` Cookie 发送，不写入快照。
   - `--insecure-loopback` 只对数字回环地址的 HTTPS 关闭证书校验。
   - `--output` 的实际落点（解析符号链接与 junction 后）只能位于本项目目录或工作区根的 `.tmp/`（即 `../../.tmp/`）下。
   - 只有位于原位置的实体 `.tmp` 才能作为允许的临时输出根；根目录链接不能授权其他目录，工作区内的生产目录也不例外。
   - 目标已存在时拒绝写入，不覆盖。重新抓快照须换文件名，或先自行移走旧快照。

2. 查武器模板 ID：

   ```sh
   uv run spt-build weapons --snapshot runtime.json --query AK-12
   ```

   `--query` 对模板 ID、英文名和内部名做不区分大小写的子串匹配，返回 `weapons` 数组，每项含 `template_id` 和 `name`。

3. 求 AK-12（`6499849fc93611967b034949`）在弹匣不少于 30 发时后坐力最低的前 3 个 build：

   ```sh
   uv run spt-build optimize --snapshot runtime.json --weapon 6499849fc93611967b034949 --objective recoil --min-capacity 30 --solutions 3 --time-limit 60
   ```

   - `solutions[i].build` 是从枪身开始的配件树，节点含 `template_id`、`name`、`slot`、`children`。
   - `solutions[i].stats` 是统计与逐件贡献，`solutions[i].proof` 是证明状态，见 [输出契约](#输出契约)。

其余例子只换参数：

```sh
# 必须带下挂、容量 ≥ 30，人机工效最高的前 2 个 build
uv run spt-build optimize --snapshot runtime.json --weapon 6499849fc93611967b034949 --objective ergo --launcher required --min-capacity 30 --solutions 2 --time-limit 60
# 同条件，后坐力收益与人机工效按 1:1 加权
uv run spt-build optimize --snapshot runtime.json --weapon 6499849fc93611967b034949 --objective weighted --recoil-weight 1 --ergo-weight 1 --launcher required --min-capacity 30 --solutions 2 --time-limit 60
# 容量 ≥ 30 的后坐力与人机工效 Pareto 前沿，最多 3 个点
uv run spt-build optimize --snapshot runtime.json --weapon 6499849fc93611967b034949 --mode pareto --min-capacity 30 --solutions 3 --time-limit 60
# AK-102 Meta 必须带下挂
uv run spt-build optimize --snapshot runtime.json --weapon 692066c72a67c15014e822f1 --objective ergo --launcher required --solutions 2 --time-limit 60
# APB Unique（基础后坐力为 0）容量 ≥ 20 的最低后坐力 build
uv run spt-build optimize --snapshot runtime.json --weapon 6964003370da0d7ae0857060 --objective recoil --min-capacity 20 --solutions 1 --time-limit 60
```

- 这六组参数的实际输出存于 [`examples/`](examples/)，结果见 [验证记录](#验证记录)。
- `--time-limit 60` 是建议值，高于样例中 AK-12 各次调用的实际耗时（23.7–50.1 s）。样例 JSON 不记录当次请求的时限，耗时与限时内能证明到哪一步随机器负载变化，重跑可能多返回或少证明一些解。

## 命令参数

所有子命令（含 `--help` 与报错）都只向 stdout 写一行 JSON，Agent 可直接解析。
数据源必须显式给出，二选一，没有默认值，也不回退到磁盘上的服务器数据库：

- `--snapshot PATH`：读取 `snapshot` 写出的 schema 2 完整快照，载入时校验 items 与 locale 的保序 SHA-256；schema 1 缺少属性顺序完整性，明确拒绝，不静默兼容。
- `--server URL --session ID [--insecure-loopback]`：每次调用实时 GET，不落盘。

`optimize` 的其余参数：

|参数|默认|说明|
|---|---|---|
|`--weapon`|必填|精确武器模板 ID。名称或部分匹配不会被自动选中，而是报 `invalid_weapon`，`error.details.candidates` 列出子串匹配的武器|
|`--objective`|`weighted`|`recoil`、`ergo` 或 `weighted`，见 [目标与评分](#目标与评分)|
|`--recoil-weight`、`--ergo-weight`|`1`|非负十进制数，`weighted` 要求至少一个为正。输出写成约分后的分数字符串，如 `0.5` 写作 `"1/2"`|
|`--min-capacity`|`0`|当前弹匣容量下限，口径见 [模型规则](#模型规则)|
|`--launcher`|`any`|`required` 要求至少一件 Launcher 类别配件，`forbidden` 禁止任何一件|
|`--mode`|`top`|`top` 或 `pareto`，见 [top 与 Pareto](#top-与-pareto)|
|`--solutions`|`3`|`top` 的 build 数或 `pareto` 的点数上限|
|`--time-limit`|`30`|整个优化调用的秒数，不含载入数据。模型构建不可中断，实际耗时可能略超|
|`--workers`|`1`|CP-SAT 搜索线程数。大于 1 时，并列配件中选中哪件可能每次不同|

Python 调用方可用 `weapon_optimizer.data.load_snapshot(path)` 得到 catalog，再调 `weapon_optimizer.optimizer.optimize(catalog, weapon_id, ...)`。
关键字参数与 CLI 同名（连字符换成下划线，`limit` 对应 `--solutions`），权重另接受 `"1/3"` 这类分数字符串。

## 输出契约

### 目标与评分

- 后坐力收益 `recoil_reduction_percent = 100 × (Rbase − Rtotal) / |Rbase|`，`Rbase = RecoilForceUp + RecoilForceBack`，输出的 `objective.recoil_reduction_definition` 记录同一定义。
  - `Rbase > 0` 时它等于 `−ΣRecoil`，即 `stats.recoil_modifier_percent` 取反。
  - `Rbase = 0` 时恒为 0，`recoil` 目标只剩并列项起作用。当前数据中的 APB Unique（`6964003370da0d7ae0857060`）与 VPO-215 Unique（`6a28487481d0b342e84458ca`）属此类，见 [apb-zero-recoil.json](examples/apb-zero-recoil.json)。
  - `Rbase < 0` 时仍朝更低的 `Rtotal` 优化，只代表模板公式，不代表实际射击手感。
- 有效人机工效 `Eeff = min(100, ergonomics_raw)`，超过 100 的部分不计收益。
- `score = recoil_weight × recoil_reduction_percent + ergo_weight × Eeff`。权重 1:1 时，1 个百分点的后坐力收益与 1 点人机工效等价。
- 每个解都报告 `score`，`recoil` 与 `ergo` 目标也用传入的权重（默认 1:1）计算它。

|`--objective`|主目标|主目标并列时|再并列时|
|---|---|---|---|
|`recoil`|最大后坐力收益|最大 `Eeff`|最少配件|
|`ergo`|最大 `Eeff`|最大后坐力收益|最少配件|
|`weighted`|最大 `score`|最大「后坐力收益 + `Eeff`」|最少配件|

`proof.objective_value` 与 `proof.best_bound` 使用主目标的单位。
以 AK-12（`RecoilForceUp` 100、`RecoilForceBack` 302、`Ergonomics` 36）为例，`recoil_modifier_percent` 为 −85 时 `Rtotal = 402 × 0.15 = 60.3`，收益 85，若 `Eeff` 为 65 则 `score` 为 150。

`stats` 字段：

- `recoil_vertical`、`recoil_horizontal`：`RecoilForceUp`、`RecoilForceBack` 各乘 `1 + ΣRecoil / 100`，`recoil_total` 为二者之和。
- `ergonomics_raw`：未封顶的人机工效，可超过 100，不设下限。`ergonomics_effective` 即 `Eeff`。
- `magazine_capacity`、`has_launcher`：容量与下挂条件的实际取值。
- `part_count`、`parts`：不含枪身的配件数与逐件明细（路径、`is_mod`、Recoil 与 Ergonomics 贡献）。

### 证明与状态

|字段|含义|
|---|---|
|`status`|`optimal`：返回的解主目标均已证明（Pareto 还要求并列项已证明），且已凑满请求数量或已证明再无更多解。`feasible`：有解，但有解主目标未证明、Pareto 有点并列项未证明，或未凑满请求数量就超时。`infeasible`：已证明没有满足硬条件的 build。`unknown`：限时内一个解也没找到|
|`search.ranking_proven`|每个解的主目标都已证明，Pareto 另要求 `tie_break_proven`|
|`search.exhausted`|已证明再无满足条件的 build（top）或更高 `Eeff` 的点（Pareto）。`status: optimal` 而 `exhausted: false` 只表示请求数量已凑满，不是完整枚举|
|`proof.status`|该解主目标 `optimal`（已证明）或 `feasible`（超时时的当前最好解）|
|`proof.scope`|该解所在的搜索范围，是否已证明看 `proof.status`。`all_builds`：全部 build（第 1 名）。`remaining_builds`：top 中为排除先前已返回的完整 build 后的剩余 build，Pareto 中为 `Eeff` 严格高于上一点的剩余区域，不代表该点非支配。`pareto_point`：Pareto 模式下主目标与并列项均已证明的前沿点|
|`proof.best_bound`、`proof.gap`|求解器给出的主目标上界与相对差距，以浮点输出，很大的精确整数目标可能被舍入。是否最优以 `proof.status` 为准，不单凭 `gap` 为 0 判断|
|`proof.original_global_optimum`|top 模式下主目标已证明且与第 1 名相等，同属全局最优|
|`proof.tie_break_proven`、`proof.minimal_parts_proven`|并列项、最少配件是否已证明|

### top 与 Pareto

- `top`：第 k 名是排除前 k − 1 个 build 后的最优解，后续名次可能比第 1 名差，用 `original_global_optimum` 区分。
  任一位置换了模板就算不同 build，所以前几名常是属性完全相同、只差颜色款或贡献总和相同的配件组合的方案。
  例如 [ak12-launcher-ergo.json](examples/ak12-launcher-ergo.json) 两个解统计相同，差别在弹匣（PMAG 30 GEN M3 与其 FDE 款）、瞄具座、枪托等部件。
- `pareto`：忽略 `--objective`，输出 `objective.kind` 为 `recoil`。
  第一个点取后坐力收益最高、其中 `Eeff` 最高的 build，之后每个点要求 `Eeff` 严格高于上一点，再求后坐力收益最高。
  每个点只给一个代表 build。遇到以下任一情况就停止，因此前沿可能不完整：
  - 已有 `--solutions` 个点。
  - 超时。
  - 某点主目标或并列项未证明：该点照常返回但不标 `pareto_point`，也不再以它为界继续搜索。
  - 已证明不存在 `Eeff` 更高的 build（`exhausted: true`）。

## 模型规则

- 候选：每个槽接受运行时数据中 Filter 允许的全部物品模板，含模组新增物品。不按库存、持有、价格、商人或解锁筛选，也不按单件属性预先剪枝，build 可能含玩家拿不到的配件。
- 槽位 Filter：类别 ID 展开为其下全部物品。一个槽的多条过滤条目须同时满足，条目内 `Filter` 任一匹配即可，`ExcludedFilter` 排除。未知 ID 不匹配任何物品。`filters` 缺失或为 `[]` 时接受全部物品，条目的 `Filter` 为 `[]` 时不接受任何物品。
- 父子依赖：配件只能装在已装上的父件槽里，父件在时 `_required` 槽必须装。同一位置（从枪身起的槽名路径）最多一件，同一模板可装在不同位置。
- 冲突：`ConflictingItems` 属性名按客户端 JSON.NET 规则忽略大小写，按 `_props` 中的键顺序后者覆盖前者，不合并；后置空数组或 `null` 会清除前值。有效数组按精确模板 ID 在整棵树（含枪身）上双向比较，不按类别展开。模板把自己列为冲突时，只禁止同时出现两件。
- 不装弹：弹药类别不作候选，包括转轮弹巢的 `camora_` 槽。
- 容量：只看枪身直系 `mod_magazine` 槽里的弹匣，下挂等其他位置的弹匣不计。普通弹匣取 `Cartridges[0]._max_count`。转轮弹巢（类别 `610720f290b75a49ff2e5e25` 即 CylinderMagazine 及其子类 SpringDrivenCylinder）取直系 `camora_` 槽数。
- 下挂：Launcher 类别（`55818b014bdc2ddc698b456b`，英文名 UBGL）的配件在任意深度都算。
- 状态：枪托展开、未装弹，不含技能、耐久、弹药及射击时的动态后坐力修正。只有 Mod 类别的子件计入属性。

属性公式与客户端（`EscapeFromTarkov_Data/Managed/Assembly-CSharp.dll`）IL 对应如下。
后坐力收益的归一化与 `Eeff` 封顶是本工具的评分规则，不改变客户端公式算出的原始模板属性。

|统计|公式|客户端依据|
|---|---|---|
|后坐力|`Rtotal = Rbase × (1 + ΣRecoil / 100)`|`Weapon.get_RecoilBase`、`get_RecoilDelta`（未折叠分支）、`get_RecoilTotal`|
|人机工效|`Ergonomics × (1 + ΣErgonomics / max(1, Ergonomics))`|`Weapon.get_ErgonomicsDelta`、`get_ErgonomicsTotal`、`Mod.get_Ergonomics`|
|冲突|双向精确模板 ID|`Slot.CheckConflictingItems`、`Item.get_ConflictingItems`|
|冲突属性解析|忽略属性名大小写、按顺序后者覆盖，不合并数组|`ItemTemplate.TemplateSerializer.Deserialize` → `_props.ToObject`；`Newtonsoft.Json.dll` 的 `JsonPropertyCollection.GetClosestMatchProperty`、`JsonSerializerInternalReader.SetPropertyValue`|
|当前弹匣|枪身直系 `mod_magazine`|`Weapon.GetCurrentMagazine`、`Weapon.GetMagazineSlot`|
|弹巢容量|直系 `camora_` 槽数|`CylinderMagazine.get_MaxCount`|
|槽位 Filter|见上|`ItemFilterExtension.CheckItem`|

基础人机工效低于 1 时仍按客户端的 `max(1, Ergonomics)` 分母计算，不替换为简单加法；例如基础值 −158、配件总和 +2 时，raw 值为 −474。

### 精确性与报错

- 实时数据与快照均保留数值的十进制精度，不经过 binary64。schema 2 的 SHA-256 基于 UTF-8、紧凑分隔符、`ensure_ascii=false` 且不排序对象键的 JSON；属性顺序影响大小写冲突键的有效值，因此仅交换键顺序也会使摘要校验失败。
- 模板小数按有理数精确计算、放大为整数求解，不模拟客户端单精度浮点的累加误差，与游戏显示可能差在末位。超出 CP-SAT 安全整数范围时报错，不做舍入。
- 可达模板之间存在循环（A 的槽能装 B，B 的槽又能装 A）时报错并列出环，不截断后给出看似最优的结果。
- 每个返回的 build 都经独立评估器重新校验槽位、依赖、冲突并重算统计后才输出。
- 无解不是错误：输出 `status: infeasible`，退出码 0。

|`error.code`|退出码|情形|
|---|---|---|
|`invalid_arguments`|2|参数非法，或模型无法精确建立（模板循环、数值超出精确整数范围）|
|`invalid_weapon`|2|`--weapon` 不是精确武器模板 ID|
|`data_error`|1|GET 失败、快照缺失或哈希不符、快照输出越界或目标已存在、模板继承链损坏|
|`internal_error`|1|其他异常|
|`interrupted`|130|被中断|

### 只读边界与结果适用范围

- 实时模式只向 `--server` 发两次 GET（`/client/items`、`/client/locale/en`），不走代理、不跟随重定向，报错信息中的会话 ID 替换为 `<redacted>`。
- 两次 GET 先后进行，不是服务器端原子快照。输出的 `source` 记录服务器、时间与 SHA-256。
- 工具唯一写入的文件是 `snapshot --output` 指定的新文件，位置限制见 [跑通第一个例子](#跑通第一个例子)。
- build 只说明运行时模板规则允许这样装，且静态公式下取得该值。它没有经过客户端实装或试射，不保证每件模组配件在客户端功能正常。
- 工具不按客户端功能是否验证过来排除下挂候选。当前数据中 AK-12 的 `mod_launcher` 槽允许 GP-34（`5648b62b4bdc2d9d488b4585`，Recoil −8、Ergonomics −32），四个 AK-12 样例的全部 8 个 build 都装了它，包括不要求下挂的 recoil 与 Pareto 样例。GP-34 模板带 `LinkedWeapon`，它在 AK-12 上的联动发射与动画尚未在客户端实测。不需要下挂时加 `--launcher forbidden`。

## 验证记录

- `uv run python -m unittest discover -s tests -v`：65 项全部通过，含小规模穷举 oracle 对照、各目标、容量与下挂组合、深层反向冲突、大小写冲突键的顺序覆盖与 self-conflict、负数基础人机工效、人机封顶、小数细差、转轮弹巢、有符号与零基础后坐力、保序快照及非法 session 零 HTTP 请求等回归。目录链接逃逸、`.tmp` 根链接到工作区内生产目录或工作区外目录的回归在本机均实际执行，未跳过。
- CLI 实测：`--help`、以名称代替模板 ID 时返回候选、`NaN` 权重报错、极短时限返回 `unknown` 与空 `solutions`。
- 向已存在的快照路径再次执行 `snapshot`：退出码 1、`data_error`（File exists），原文件前后 SHA-256 相同。
- `snapshot` 实际读取运行时数据，最终 Decimal 版本仍为 7084 个物品模板、39805 条英文本地化，`weapons --query AK-12` 查询成功。
- 后续验证仅使用已有完整快照离线进行：先核对旧摘要，再明确转换为 schema 2，保留旧来源元数据而不伪造新的 GET。现有 6 份示例的 9 个 build 均通过新增冲突规则校验；当前数据只增加约束、不移除旧约束，原已证明且仍合法的最优解保留原证明，原未证明项不升级。
- 非法 session 的 CLI smoke 指向非生产端口 `https://127.0.0.1:1`：退出码 2、`invalid_arguments`，API 与 CLI 回归均确认 HTTP 方法从未被调用。
- schema 2 离线 CLI smoke：含非标准大小写冲突键的隔离 fixture 选择合法的 `safe` 配件而非冲突的高收益候选；现有完整快照上的 APB 返回 `optimal`、`Rtotal=0`、有效人机 100、4 件配件。
- 隔离文件系统 smoke：普通 `.tmp` 输出成功；把隔离安装的 `.tmp` 根链接到其模拟生产目录后，保存被拒绝且目标文件不存在；未改动真实 `.tmp` 根或生产目录。
- 下表是 `examples/` 中六份输出的实际结果，请求时限未记录在 JSON 中，耗时取 `search.elapsed_seconds`。
- 六份示例保留采集时的 `source.hash_format` 与旧版 sorted-key `content_sha256`，不能与新运行的保序摘要直接比较，也不能仅据差异判定数据变化。

|#|样例|请求|结果|
|---|---|---|---|
|1|[ak12-recoil.json](examples/ak12-recoil.json)|recoil，容量 ≥ 30，前 3|45.0 s 返回 2 个，顶层 `feasible`，`ranking_proven: true`。#1：ΣRecoil −85%，纵向 15，后向 45.3，人机 65，30 发，28 件，主目标、并列项、最少配件均已证明。#2：属性相同，32 件，并列项未证明。第 3 个在限时内未求出|
|2|[ak12-launcher-ergo.json](examples/ak12-launcher-ergo.json)|ergo，必须下挂，容量 ≥ 30，前 2|39.8 s，`optimal`，两解均全部证明。人机 raw 100.3、有效 100，ΣRecoil −56%（纵向 44，后向 132.88），30 发，31 件|
|3|[ak12-launcher-weighted.json](examples/ak12-launcher-weighted.json)|weighted 1:1，必须下挂，容量 ≥ 30，前 2|23.7 s，`optimal`，两解均全部证明。`score` 165.5，ΣRecoil −76.5%（纵向 23.5，后向 70.97），人机 89，30 发，27 件|
|4|[ak12-pareto.json](examples/ak12-pareto.json)|pareto，容量 ≥ 30，最多 3 点|50.1 s 返回 2 个，顶层 `feasible`。点 1：ΣRecoil −85%（纵向 15），人机 65，`pareto_point`。候选 2：ΣRecoil −84.5%（纵向 15.5），人机 70，主目标已证明但并列项在限时内未证明，标 `remaining_builds`，不算已证明的 Pareto 点|
|5|[ak102-launcher-infeasible.json](examples/ak102-launcher-infeasible.json)|AK-102 Meta，ergo，必须下挂，前 2|0.67 s 证明 `infeasible`，`exhausted: true`。该枪 `mod_launcher` 槽的 Filter 为空，整棵树装不上 Launcher|
|6|[apb-zero-recoil.json](examples/apb-zero-recoil.json)|APB Unique，recoil，容量 ≥ 20，前 1|0.047 s，`optimal`，主目标、并列项、最少配件均已证明。`Rbase` 为 0，后坐力收益与 `Rtotal` 都是 0。人机 raw 168、有效 100，50 发，4 件，`score` 100|
