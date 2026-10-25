# 枪械 Build 优化器

> Parent: 无
> Audience: user — 使用产物的人或 agent
> Genre: manual — 使用者怎么用产物
> Visibility: local — 仅供本机使用，可依赖本机环境与配置
> Maintain: living — 持续更新，保持内容有效

`spt-build` 是供 Agent 调用的只读命令行工具，它输出的配件树也便于玩家在游戏里照着找件。
给定一把枪的精确模板 ID，它在 SPT 运行时物品数据中搜索完整配件树（虚拟 build），求后坐力最低、人机工效最高或二者加权最优的方案，可附加弹匣容量下限、下挂（枪挂榴弹发射器）和消音器硬条件。
默认只选用有可核对的非随机购买来源（商人、跳蚤或买整枪拆件）的配件，JSON 结果逐件记录来源与解锁条件。
这些来源可能要求商人信赖等级、任务等条件，工具不读取角色进度和当前库存。
结果默认以一行 JSON 写到 stdout，也可输出带中文部位名和游戏内名称的配件树。
工具不读写存档、不改模组或服务器数据、不启停服务器，也不在游戏里装配或试射。

## 准备环境

- 安装 [uv](https://docs.astral.sh/uv/)，Python 3.12 由 uv 按需获取。
- 采集本地证据需要本机 SPT 安装根目录，即本项目的上两级目录，以下命令写作 `../..`。
- 重新采集运行时数据时，本机 SPT 服务器需已在 `https://127.0.0.1:6969` 运行。

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
|[.gitignore](.gitignore)|保留 GitHub 的 Python 模板，来源链接见文件头|忽略本机环境、临时产物和运行时快照，保留 `uv.lock` 与 `examples/` 中的 JSON 示例|
|[.gitattributes](.gitattributes)|自动识别文本／二进制，使用内置 Python diff driver|文本在仓库与工作区统一为 LF，不转换二进制内容|
|[.editorconfig](.editorconfig)|项目边界、UTF-8、LF、末尾换行及通用空白规则|Python／TOML 四空格、JSON 两空格，保留 Markdown 行尾空格|

- 应提交 `uv.lock`、`examples/*.json` 与 `builds/*.json`。
- 完整运行时快照使用被忽略的 `runtime*.json`／`*snapshot*.json` 文件名，或放入 `snapshots/`。

## 跑通第一个例子

1. 准备完整快照。`--snapshot` 只接受 schema 3 快照，它包含运行时物品、英文与中文名称、获取性证据和兼容性证据。

   - 本机已有离线快照 `snapshots/ak101-installation-v3.json`，含全部本地证据，可直接用于下面的步骤。
   - 重新采集运行时数据与本地证据：

     ```sh
     uv run spt-build snapshot --server https://127.0.0.1:6969 --insecure-loopback --session SESSION_ID --installation ../.. --output snapshots/runtime-new.json
     ```

     - `SESSION_ID` 换成调用方提供的 SPT 存档 ID（24 位十六进制）。非法值在任何 HTTP 请求前被拒绝，工具不使用默认或猜测的会话。有效值作为 `PHPSESSID` Cookie 发送，不写入快照。
     - `--insecure-loopback` 只对数字回环地址的 HTTPS 关闭证书校验。
     - `--installation` 读取的内容见 [本地证据](#本地证据)。

   - 已有不含本地证据的 schema 3 快照时，用 `prepare` 离线补采，写到新文件：

     ```sh
     uv run spt-build prepare --snapshot snapshots/runtime-plain.json --installation ../.. --output snapshots/runtime-local.json
     ```

   - `snapshot` 与 `prepare` 写文件时：
     - `--output` 的实际落点（解析符号链接与 junction 后）只能位于本项目目录或工作区根的 `.tmp/`（即 `../../.tmp/`）下。
     - 只有位于原位置的实体 `.tmp` 才能作为允许的临时输出根，根目录链接不能授权其他目录，工作区内的生产目录也不例外。
     - 目标已存在时拒绝写入，不覆盖。重新采集须换文件名，或先自行移走旧快照。
   - schema 1、2 的旧快照会被拒绝。`examples/*.json` 与 `builds/*.json` 是求解结果，不能用作 `--snapshot`。

2. 查武器模板 ID：

   ```sh
   uv run spt-build weapons --snapshot snapshots/ak101-installation-v3.json --query AK-101
   ```

   `--query` 对模板 ID、英文名和内部名做不区分大小写的子串匹配，返回 `weapons` 数组，每项含 `template_id` 和英文 `name`。

3. 求 AK-101 Unique（`692066c72a67c15014e8216c`）带下挂、弹匣不少于 30 发时，后坐力收益与人机工效 1:1 加权最优的 build，把 JSON 结果存到工作区根的临时目录：

   ```sh
   mkdir -p ../../.tmp/weapon-optimizer
   uv run spt-build optimize --snapshot snapshots/ak101-installation-v3.json --weapon 692066c72a67c15014e8216c --launcher required --min-capacity 30 --solutions 1 --time-limit 60 > ../../.tmp/weapon-optimizer/ak101.json
   ```

   - 未写出的参数取默认值：`--objective weighted`、两个权重均为 `1`、`--acquisition required`。
   - `solutions[i]` 含配件树 `build`、统计 `stats`、证明状态 `proof` 和逐件来源与条件 `acquisition`，见 [输出契约](#输出契约)。
   - 时限内未证明最优时 `proof.status` 为 `feasible`，可加大 `--time-limit` 或 `--workers` 后重跑。
   - 只想看树时，去掉重定向并加 `--format tree`，直接在终端输出。
   - 还要求装消音器时加 `--require-suppressor`，其余条件不变。六款 Hexagon Cheese Grater 护木在获取性证据中没有来源，默认已被排除，不需要额外参数：

     ```sh
     uv run spt-build optimize --snapshot snapshots/ak101-installation-v3.json --weapon 692066c72a67c15014e8216c --launcher required --min-capacity 30 --require-suppressor --solutions 1 --time-limit 60 > ../../.tmp/weapon-optimizer/ak101-suppressor.json
     ```

4. 把保存的结果画成中文配件树：

   ```sh
   uv run spt-build render --snapshot snapshots/ak101-installation-v3.json --build ../../.tmp/weapon-optimizer/ak101.json
   ```

   - 树的每行是 `[部位] 缩略名〔全名〕`，例如 `[护木] B10M+B19〔Zenit B-10M 导轨护木+ B-19 上导轨组合〕`，规则见 [配件树](#配件树)。
   - `render` 输出前会按数据源重新校验结果，见 [命令参数](#命令参数)。

## 命令参数

- `snapshot`、`prepare`、`weapons` 与默认的 `optimize` 向 stdout 写一行 JSON，Agent 可直接解析。
- `optimize --format tree` 与 `render` 写纯文本配件树。
- `--help` 与所有报错始终是一行 JSON。

|子命令|作用|
|---|---|
|`snapshot`|实时 GET 运行时数据并写完整快照，加 `--installation` 时同时采集本地证据|
|`prepare`|给已有 schema 3 快照离线采集本地证据，写到新文件|
|`weapons`|按子串列出武器模板 ID 与英文名，不替调用方选枪|
|`optimize`|求最优 build|
|`render`|按数据源校验 `optimize` 保存的 JSON 结果，再画成配件树|

`weapons`、`optimize`、`render` 的数据源必须显式给出，二选一，没有默认值，也不回退到磁盘上的服务器数据库：

- `--snapshot PATH`：读取 schema 3 完整快照，载入时校验 items、locale、locale_ch、acquisition、compatibility 五部分各自的保序 SHA-256。
- `--server URL --session ID [--insecure-loopback]`：每次调用实时 GET，不落盘，也不含本地证据。

三者都可另加 `--installation ROOT`，现场从本机安装目录读取本地证据，替换数据源中已保存的中文名称、获取性与兼容性证据。

`optimize` 的其余参数：

|参数|默认|说明|
|---|---|---|
|`--weapon`|必填|精确武器模板 ID。名称或部分匹配不会被自动选中，而是报 `invalid_weapon`，`error.details.candidates` 列出子串匹配的武器|
|`--objective`|`weighted`|`recoil`、`ergo` 或 `weighted`，见 [目标与评分](#目标与评分)|
|`--recoil-weight`、`--ergo-weight`|`1`|非负十进制数，`weighted` 要求至少一个为正。输出写成约分后的分数字符串，如 `0.5` 写作 `"1/2"`|
|`--min-capacity`|`0`|当前弹匣容量下限，口径见 [模型规则](#模型规则)|
|`--launcher`|`any`|`required` 要求至少一件 Launcher 类别配件，`forbidden` 禁止任何一件|
|`--require-suppressor`|关闭|要求树中装有消音器，口径见 [模型规则](#模型规则)。输出的 `constraints.require_suppressor` 记录是否开启|
|`--acquisition`|`required`|`required` 只用有已记录获取来源的配件，数据源缺少获取性证据时报错。`templates` 显式跳过获取性检查，只用于模板研究，结果不代表能买到|
|`--mode`|`top`|`top` 或 `pareto`，见 [top 与 Pareto](#top-与-pareto)|
|`--solutions`|`3`|`top` 的 build 数或 `pareto` 的点数上限|
|`--time-limit`|`30`|整个优化调用的秒数，不含载入数据。模型构建不可中断，实际耗时可能略超|
|`--workers`|`1`|CP-SAT 搜索线程数。大于 1 时，并列配件中选中哪件可能每次不同|
|`--format`|`json`|`json` 或 `tree`|
|`--language`|`ch`|配件树的语言。`ch` 逐条目回退英文，`en` 只用英文|

`render` 的其余参数：

- `--build PATH`：`optimize` 输出的 JSON 结果文件。
- `--solution N`：只画第 N 个解（从 1 开始），省略时画全部解。
- `--language ch|en`：同 `optimize`。

`render` 先用所给数据源重新校验每个解，任一项不符就输出 JSON 报错，不输出树，错误码见 [精确性与报错](#精确性与报错)：

- 装配合法，冲突按当前 [兼容层](#兼容层) 判断。
- 重算的 `stats` 与文件中保存的完全一致。
- 结果的 `constraints.acquisition` 为 `required` 时，每件配件在数据源的获取性证据中都有已记录来源。
- 结果的 `constraints.require_suppressor` 为 `true` 时，每个解的树中都装有消音器。没有该字段的已有结果按未要求处理，仍可渲染。
- 结果的 `solve_context` 与按所给数据源重算的完全一致，含义见 [证明上下文](#证明上下文)。缺少该字段的旧结果（含全部 [历史样例](#历史样例与验证记录)）或上下文不符时，需用所选数据源重新 `optimize`。

Python 调用方：

- `weapon_optimizer.data.load_snapshot(path)` 得到 catalog，需要现场读取本地证据时再经 `weapon_optimizer.local.enrich_catalog(catalog, installation_root)`。
- `weapon_optimizer.optimizer.optimize(catalog, weapon_id, ...)` 的关键字参数与 CLI 同名（连字符换成下划线，`limit` 对应 `--solutions`），权重另接受 `"1/3"` 这类分数字符串。
- `weapon_optimizer.evaluate.evaluate_build(catalog, build, *, require_suppressor=False)` 独立校验一棵树并返回 `stats`，`require_suppressor=True` 时树中没有消音器即抛出 `ValueError`。
- `weapon_optimizer.presentation.render_result(catalog, result, language="ch", rank=None)` 返回配件树文本，不做上述 `render` 校验。

## 本地证据

`--installation ROOT` 只读下表位置，不读存档目录，不执行模组代码：

|证据|读取位置|用途|
|---|---|---|
|中文名称|`SPT_Runtime/SPT_Data/database/locales/global/ch.json`，以及 `SPT_Runtime/user/mods/*/db/CustomLocales/ch.json`、`ch.jsonc`|配件树的中文缩略名与全名|
|获取性|`SPT_Runtime/SPT_Data/configs/` 的 `item.json`、`ragfair.json`，`SPT_Runtime/SPT_Data/database/` 的 `globals.json`、`templates/handbook.json`、`templates/prices.json`、`traders/`，以及 `SPT_Runtime/user/mods/*/db/` 下的 JSON／JSONC|候选筛选与逐件来源，见 [获取性规则](#获取性规则)|
|兼容性|`SPT_Runtime/user/mods/TGC-NG/TGC-NG.dll` 与同模组的 `db/CustomItems/modTGC_items.json`|克隆件冲突传播，见 [兼容层](#兼容层)|

- 中文名称只取物品模板的 `Name`、`ShortName` 条目。模组条目覆盖基础条目，两个模组对同一条目给出不同文本时报错，不猜加载顺序。
- 每个证据文件的相对路径与 SHA-256 记录在快照中（`source.local_context`、`acquisition.source_files`、`compatibility.sources`）。
- 获取性与兼容性证据绑定物品数据的摘要，与所用物品数据不符时报错。
- 证据反映采集时的文件。更新模组后，应在服务器运行时用 `snapshot --installation` 重新采集，让运行时物品与本地证据出自同一状态。

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

- `recoil_vertical`、`recoil_horizontal`：`RecoilForceUp`、`RecoilForceBack` 各乘 `1 + ΣRecoil / 100`，配件树中显示为垂直、水平后坐力。`recoil_total` 为二者之和。
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

### 证明上下文

`optimize` 结果中的 `solve_context` 记录证明所依赖的搜索范围，`render` 据此判断保存的证明能否沿用：

- `version`：模型语义版本，语义改变时递增。
- `items_sha256`：物品数据的摘要。配色变体冲突规则由物品数据算出，也随之绑定。
- `compatibility_sha256`：兼容层规则版本与克隆冲突传播规则的摘要。
- `acquisition`：获取性模式。
- `allowed_templates_sha256`：`required` 模式下可选配件集合的摘要，`templates` 模式为 `null`。

名称（中英文 locale）与抓取时间不在其中，更换它们不影响证明。

### top 与 Pareto

- `top`：第 k 名是排除前 k − 1 个 build 后的最优解，后续名次可能比第 1 名差，用 `original_global_optimum` 区分。
  任一位置换了模板就算不同 build，所以前几名常是属性完全相同、只差颜色款或贡献总和相同的配件组合的方案。
  例如历史样例 [ak12-launcher-ergo.json](examples/ak12-launcher-ergo.json) 两个解统计相同，差别在弹匣（PMAG 30 GEN M3 与其 FDE 款）、瞄具座、枪托等部件。
- `pareto`：忽略 `--objective`，输出 `objective.kind` 为 `recoil`。
  第一个点取后坐力收益最高、其中 `Eeff` 最高的 build，之后每个点要求 `Eeff` 严格高于上一点，再求后坐力收益最高。
  每个点只给一个代表 build。遇到以下任一情况就停止，因此前沿可能不完整：
  - 已有 `--solutions` 个点。
  - 超时。
  - 某点主目标或并列项未证明：该点照常返回但不标 `pareto_point`，也不再以它为界继续搜索。
  - 已证明不存在 `Eeff` 更高的 build（`exhausted: true`）。

### 获取性字段

- `constraints.acquisition`：本次的获取性模式，`required` 或 `templates`。
- `solutions[i].acquisition`：每件配件一条，不含枪身，字段为 `template_id`、`slot`、`path`、`status`、`sources`、`blocked_channels`、`conditions`。`required` 模式下 `status` 只会是 `available` 或 `conditional`。
- `templates` 模式下每件的 `status` 都是 `not_checked`，`sources` 为空，另有说明未检查的 `reason`。
- 文本树只显示总体获取性摘要；逐件渠道、等级、任务与库存条件见结果 JSON 的 `solutions[i].acquisition`，不会逐件附在树后。

状态、渠道与条件的含义见 [获取性规则](#获取性规则)。

### 配件树

每个解输出一段：

1. 标题 `配装 #名次｜状态：…`，`proof.status` 不是 `optimal` 时追加「（未证明最优）」。
2. 摘要：有效人机工效、垂直与水平后坐力、弹匣发数。结果要求消音器时末尾追加 `｜枪口要求：消音器子类型`。
3. 获取性模式：`required` 显示已按记录来源筛选、未读取角色解锁状态，其他情况显示「获取性：未筛选，仅供模板研究。」。
4. 配件树，枪身为第一行，部位写作 `[武器]`。

每行格式为 `[部位] 缩略名〔全名〕`：

- 部位：槽位名去掉末尾编号后映射为中文，如 `mod_stock_001` 显示为「枪托」，`mod_mount_002` 显示为「基座」。未收录的槽位显示原槽名，如 `[mod_custom_001]`。映射表是 [presentation.py](src/weapon_optimizer/presentation.py) 中的 `_SLOT_LABELS`。
- 名称按模板 ID 精确查 locale 条目，去掉富文本标签，不自行翻译，缺中文时也不加标注：
  - `ch` 的缩略名依次取中文 `ShortName`、英文 `ShortName`、中文 `Name`、英文 `Name`。
  - `ch` 的全名依次取中文 `Name`、英文 `Name`。
  - `en` 只取英文条目。
  - 所有条目都缺时用模板内部名 `_name`。
- 数据源没有中文名称证据时，`ch` 全部回退为英文。
- 树保持输入的装配结构，同一模板装在不同位置时各占一行。
- 没有解时只输出 `状态：…；没有可交付方案。`。

## 模型规则

- 候选：每个槽接受 Filter 允许的物品模板，含模组新增物品，不按单件属性预先剪枝。
  - 默认 `required` 模式只保留获取性状态为 `available` 或 `conditional` 的配件，见 [获取性规则](#获取性规则)。
  - `templates` 模式保留全部 Filter 允许的模板，build 可能含玩家拿不到的配件。
  - 枪身由调用方指定，不检查其获取性。
  - 必须装的子槽没有可选候选时，其父件也不会入选。
- 槽位 Filter：类别 ID 展开为其下全部物品。一个槽的多条过滤条目须同时满足，条目内 `Filter` 任一匹配即可，`ExcludedFilter` 排除。未知 ID 不匹配任何物品。`filters` 缺失或为 `[]` 时接受全部物品，条目的 `Filter` 为 `[]` 时不接受任何物品。
- 父子依赖：配件只能装在已装上的父件槽里，父件在时 `_required` 槽必须装。同一位置（从枪身起的槽名路径）最多一件，同一模板可装在不同位置。
- 冲突：
  - `ConflictingItems` 属性名按客户端 JSON.NET 规则忽略大小写，按 `_props` 中的键顺序后者覆盖前者，不合并。后置空数组或 `null` 会清除前值。
  - 有效数组按精确模板 ID 在整棵树（含枪身）上双向比较，不按类别展开。模板把自己列为冲突时，只禁止同时出现两件。
  - 在此之上叠加 [兼容层](#兼容层) 的规则，求解器与独立评估器使用同一份冲突关系。
- 不装弹：弹药类别不作候选，包括转轮弹巢的 `camora_` 槽。
- 容量：只看枪身直系 `mod_magazine` 槽里的弹匣，下挂等其他位置的弹匣不计。普通弹匣取 `Cartridges[0]._max_count`。转轮弹巢（类别 `610720f290b75a49ff2e5e25` 即 CylinderMagazine 及其子类 SpringDrivenCylinder）取直系 `camora_` 槽数。
- 下挂：Launcher 类别（`55818b014bdc2ddc698b456b`，英文名 UBGL）的配件在任意深度都算。
- 消音器：开启 `--require-suppressor` 时，树中任意深度至少有一件配件按类别继承属于 Silencer（`550aa4cd4bdc2dd8348b456c`）或其子类别。
  - 只按类别判断，不看名称或 `IsSilenced` 属性。
  - 消音器可以装在任何合法的转接器、制退器或枪管上，这些中间件不要求也是消音器。
  - `LinkedWeapon` 指向的联动武器不算树的一部分。
  - 不改变候选集合，与容量、下挂、获取性和兼容层规则同时生效。没有同时满足这些条件的消音器时结果为 `infeasible`，不会放宽条件。
  - `stats` 不另设消音器字段。
- 状态：枪托展开、未装弹，不含技能、耐久、弹药及射击时的动态后坐力修正。只有 Mod 类别的子件计入属性。

属性公式与客户端（`EscapeFromTarkov_Data/Managed/Assembly-CSharp.dll`）IL 对应如下。
后坐力收益的归一化与 `Eeff` 封顶是本工具的评分规则，不改变客户端公式算出的原始模板属性。

|统计|公式|客户端依据|
|---|---|---|
|后坐力|`Rtotal = Rbase × (1 + ΣRecoil / 100)`|`Weapon.get_RecoilBase`、`get_RecoilDelta`（未折叠分支）、`get_RecoilTotal`|
|人机工效|`Ergonomics × (1 + ΣErgonomics / max(1, Ergonomics))`|`Weapon.get_ErgonomicsDelta`、`get_ErgonomicsTotal`、`Mod.get_Ergonomics`|
|冲突|双向精确模板 ID|`Slot.CheckConflictingItems`、`Item.get_ConflictingItems`|
|冲突属性解析|忽略属性名大小写、按顺序后者覆盖，不合并数组|`ItemTemplate.TemplateSerializer.Deserialize` → `_props.ToObject`，以及 `Newtonsoft.Json.dll` 的 `JsonPropertyCollection.GetClosestMatchProperty`、`JsonSerializerInternalReader.SetPropertyValue`|
|当前弹匣|枪身直系 `mod_magazine`|`Weapon.GetCurrentMagazine`、`Weapon.GetMagazineSlot`|
|弹巢容量|直系 `camora_` 槽数|`CylinderMagazine.get_MaxCount`|
|槽位 Filter|见上|`ItemFilterExtension.CheckItem`|

基础人机工效低于 1 时仍按客户端的 `max(1, Ergonomics)` 分母计算，不替换为简单加法。例如基础值 −158、配件总和 +2 时，raw 值为 −474。

### 获取性规则

获取性按 SPT 4.1 的商人供货与动态跳蚤生成规则，从本地证据离线判断某个模板是否有已记录的来源。

|`status`|含义|`required` 模式|
|---|---|---|
|`available`|至少一个来源没有附加条件|可选|
|`conditional`|每个来源都带条件，如商人信赖等级、任务或跳蚤等级|可选|
|`unproven`|建模的渠道里都没找到来源，不代表游戏中一定拿不到|排除|

|`channel`|来源|
|---|---|
|`dynamic_flea`|SPT 动态跳蚤可以生成该物品，不要求有商人供货或战利品来源|
|`trader`|商人货架直接出售|
|`trader_dismantle`|商人出售的组合件上可拆下的子件|
|`dynamic_preset_dismantle`|动态跳蚤可生成的武器预设上可拆下的子件|

- `dynamic_flea` 只覆盖武器与 Mod 类别，需同时满足以下条件，不满足的原因记在 `blocked_channels`：
  - 不在全局物品黑名单中，包括 `configs/item.json` 与模组声明的 `addtoItemBlacklist`。
  - 不是任务物品，也不属于 SPT 排除的动态生成基础类别。
  - 有正的已登记价格（handbook、prices 或模组声明的价格）。
  - 跳蚤黑名单设置 `enableBsgList` 开启时，运行时物品的 `CanSellOnRagfair` 为 `true`。
  - 不在跳蚤自定义黑名单或直接父类别黑名单中。
  - 跳蚤已启用，且该类别的动态报价数量上限为正。
- 商人来源取自基础与模组的 `assort`、`questassort`、`CustomItems` 的 `addtoTraders`、`CustomAssortSchemes`。报价需有以物易物方案和信赖等级记录，且配置库存为正或无限。
  - 一个以物易物方案中，每项需求都须指向现有物品模板且数量为正，否则该方案不算。
  - 组合件上标为不可拆（`IsUnremovable`、`IsLockedafterEquip`）的子件不算来源。
  - 多个模组对同一报价给出不同设置且无法确定覆盖顺序时报错，不猜测。
- 黑名单只关闭动态跳蚤渠道，同一物品已证实的商人来源仍然有效。
- `conditions` 中的条件类型：跳蚤开放等级与分级跳蚤、生成库存、商人信赖等级、以物易物、商人库存、商人解锁、任务（含 `questassort` 状态）、任务锁定的交易。
- 条件只描述配置要求。工具不读取角色，各条件带 `current_character_checked: false` 等标记，等级、任务进度、当前库存与实时报价都未检查。
- 未建模的渠道：制作、任务奖励、玩家库存、活动与实时报价，以及模组 DLL 在运行时对数据的改动。

方案只采用上述渠道表中能买到指定物品的来源。
付钱或开箱后随机决定得到什么的来源（如开包、盲盒）以及战利品不作为来源，方案不依赖抽中特定配件。
只能从这类途径取得的配件状态为 `unproven`，默认被排除，但这不表示它在游戏中拿不到。

例如 DefinitiveWeaponVariants（DWV）的 Universal Variant Weapon Core（v1.0 为 `69ea242e87263e6f788fabe4`）：

- DWV 生成这些核心时写入的描述是「Only obtainable from Unknown Variant Weapon Core Packages」，并把各版本放进该包的抽取池。没有商人直接出售 Universal 核心。
- Unknown Variant Weapon Core Package 由 Skier 在信赖等级 1 出售，价格 250000 卢布，每包随机开出 5 个核心，包本身也会按概率出现在战利品中，配置见 DWV 的 `config/config.jsonc` 中 `UnknownPackage`。
- DWV 生成变体武器预设时，`mod_core` 装的是与该变体品质对应的核心（`WeaponGenerator.GenerateWeaponsFromVariantConfig`），不是 Universal 核心，因此不把它当作随枪附带的配件。

### 兼容层

原始模板不被修改。兼容层在客户端的精确模板 ID 冲突之外补两类规则，补足模组内容在冲突表上的遗漏：

- 克隆件冲突传播：本地证据含 TGC-NG 时启用。
  - `TGC-NG.dll` 的 SHA-256 必须与已检查过 `CopyToFilter` 逻辑的版本一致，否则报错，需重新检查该版本的传播逻辑。
  - 某模板把原件列为冲突时，该冲突同样作用于 `modTGC_items.json` 中以 `itemTplToClone` 克隆它的模板，克隆的克隆同理。
  - TGC 自身克隆件声明的冲突不向外传播，与 `CopyToFilter` 排除自身物品的行为一致。
  - 数据源没有兼容性证据时不做此传播。
- 配色变体冲突合并：总是从物品数据计算，不需要本地证据。
  - 同组条件：`_parent` 相同，Prefab 路径去掉 `_black`、`_fde`、`_red`、`_ylw` 等配色后缀后相同，组内有一件无后缀的基础款，除名称、描述、背景色、`ConflictingItems`、Prefab 外的全部属性以及 Prefab 的其他字段完全一致。
  - 同组成员合并各自声明的冲突，指向任一成员的冲突也作用于全组。
  - 本规则不让同组成员彼此冲突，也不合并属性。

例如在当前数据中：

- Phase5 UMS 枪托的黑、红款都列有 MOE 托垫及三款 Lead Star Arms Ravage 枪托的冲突，黄色款遗漏这四个目标。
- MOE Carbine stock (Modded) 是 TGC 从原版 MOE Carbine stock 克隆的模板，三款 UMS 都没有把它列为冲突。
- 配色规则把这四项冲突补到黄色款，克隆规则把 UMS 对原版 MOE 的冲突传到 Modded 款；三款 UMS 都不能与这些目标同装。

### 精确性与报错

- 实时数据与快照均保留数值的十进制精度，不经过 binary64，JSON 输出中的获取性条件数值也保留原文。
- schema 3 快照的 SHA-256 基于 UTF-8、紧凑分隔符、`ensure_ascii=false` 且不排序对象键的 JSON。属性顺序影响大小写冲突键的有效值，因此仅交换键顺序也会使摘要校验失败。
- 模板小数按有理数精确计算、放大为整数求解，不模拟客户端单精度浮点的累加误差，与游戏显示可能差在末位。超出 CP-SAT 安全整数范围时报错，不做舍入。
- 可达模板之间存在循环（A 的槽能装 B，B 的槽又能装 A）时报错并列出环，不截断后给出看似最优的结果。
- 每个返回的 build 都经独立评估器重新校验槽位、依赖、冲突并重算统计后才输出。
- 无解不是错误：输出 `status: infeasible`，退出码 0。

|`error.code`|退出码|情形|
|---|---|---|
|`invalid_arguments`|2|参数非法、会话 ID 格式不对，模型无法精确建立（模板循环、数值超出精确整数范围），或 `render` 的结果装配不合法、含冲突、缺少要求的消音器|
|`invalid_weapon`|2|`--weapon` 不是精确武器模板 ID|
|`data_error`|1|GET 失败，快照缺失、不是 schema 3 或哈希不符，快照输出越界或目标已存在，模板继承链损坏，`required` 模式缺少获取性证据，证据与物品数据不符，本地证据无法读取或无法确定覆盖顺序，TGC 程序集版本未经检查，`render` 的结果文件无法读取、名次越界、统计不符、配件没有已记录来源，或 `solve_context` 缺失、与数据源不符|
|`internal_error`|1|其他异常|
|`interrupted`|130|被中断|

### 只读边界与结果适用范围

- 实时模式只向 `--server` 发两次 GET（`/client/items`、`/client/locale/en`），不走代理、不跟随重定向，报错信息中的会话 ID 替换为 `<redacted>`。
- 两次 GET 先后进行，不是服务器端原子快照。输出的 `source` 记录服务器、时间与 SHA-256。
- 本地文件只读 [本地证据](#本地证据) 所列位置。
- 工具唯一写入的文件是 `snapshot` 或 `prepare` 的 `--output` 指定的新文件，位置限制见 [跑通第一个例子](#跑通第一个例子)。
- 默认模式的 build 说明运行时模板规则允许这样装、静态公式下取得该值，且每件配件有已记录的获取来源。它不保证当前角色已满足解锁条件或商品有货。
- build 没有经过客户端实装或试射，不保证每件模组配件在客户端功能正常。工具也不按客户端功能是否实测来排除候选。

## 当前版本验证

2026-10-25 使用保留的运行时数据离线验证，没有请求生产服务端、读取人物存档或修改游戏文件。

|检查|实际结果|
|---|---|
|完整回归|`uv run python -m unittest discover -s tests`：119 项通过，包含消音器约束与保存结果重验|
|正式 `prepare`|已生成 `snapshots/ak101-installation-v3.json`，7084 个模板、39805 条英文名称，另含中文名称与获取性、兼容性证据|
|原始数据保留|schema 2 原文件未覆盖，原文件 SHA-256 为 `fb26e7cf5053ac776653a78bc11be8b7de5e5f6b694f3c8b7ea981a852eab9c3`；items 保序摘要仍为 `313e8c3fc71ed17b6d588327b4ff58d5362483725395110b1adad5e05576856f`|
|真实兼容关系|黑、红、黄三款 UMS 均与 MOE 托垫及 TGC Modded MOE 互斥；原始黄色款遗漏的托垫冲突已进入有效关系|
|正式 `optimize`|AK-101 Unique，weighted 1:1、容量 ≥ 30、必须下挂、默认获取性筛选、单线程、请求 60 s；求解报告耗时 62.438 s，主目标 145.5 与并列优先已证明，最少配件数未证明|
|结果复核|GP-25、35 发弹匣、19 件配件；六款 Cheese Grater、GP-34、随机包通用核心均未入选；逐件来源与重算统计通过|
|正式树输出|新结果经 `render` 成功输出完整树；另以 APB 在 `optimize --format tree` 路径实际取得完整树|
|消音器约束|实际离线 CLI 已验证默认制退器、开启后转接器挂消音器子类、无可获取消音器时无解，以及原有未要求结果仍能渲染；完整证据见[消音器验证记录](../../docs/reports/2026-10-25-weapon-optimizer-suppressor.md#验证)|

当前 AK-101 结果保存在 [ak101-smg-wwii-unique-gp25-obtainable.json](builds/ak101-smg-wwii-unique-gp25-obtainable.json)。其 19 件配件均有配置支持的动态跳蚤来源，跳蚤开放等级为 15，单件附加等级为 0，分级跳蚤关闭；这不是当前报价或库存保证。

复现已保存结果的显示，不必重新求解：

```sh
uv run spt-build render --snapshot snapshots/ak101-installation-v3.json --build builds/ak101-smg-wwii-unique-gp25-obtainable.json
```

要求消音器的 AK-101、GP-25、容量 ≥ 30 结果另存为 [ak101-smg-wwii-unique-gp25-suppressed.json](builds/ak101-smg-wwii-unique-gp25-suppressed.json)，没有覆盖上面的未要求结果；完整参数、数值与恢复依据见[消音器硬约束记录](../../docs/reports/2026-10-25-weapon-optimizer-suppressor.md)。

修改范围、恢复依据与实机验证边界见[本地修复记录](../../docs/reports/2026-10-25-weapon-optimizer-usability.md)。

## 历史样例与验证记录

本节的结果都产生于获取性筛选和兼容层加入之前：求解时不筛选获取性，相当于现在的 `--acquisition templates`，也没有兼容层的冲突规则。
它们只说明当时模板规则下的最优值，不能当作可购买或可直接装配的方案。

- `examples/` 的六份样例见下表。四个 AK-12 样例的全部 8 个 build 都装了 GP-34（`5648b62b4bdc2d9d488b4585`）。GP-34 在本地全局物品黑名单 `SPT_Runtime/SPT_Data/configs/item.json` 中，基础商人货架也没有它。
- `builds/` 中的另两份历史 AK-101 Unique 结果由一次性脚本求得，`constraints` 中的 `launcher_template_id`（固定 GP-25 Kostyor）和 `excluded_template_ids`（第二份排除六款 Hexagon Cheese Grater 护木）不是 CLI 参数。两份都在约 60 s 后停在 `feasible`，主目标未证明。
  - [ak101-smg-wwii-unique-gp25-no-cheese-grater.json](builds/ak101-smg-wwii-unique-gp25-no-cheese-grater.json) 含 Phase5 UMS（黄色）、MOE Carbine stock (Modded) 与 MOE 橡胶托垫，正是 [兼容层](#兼容层) 例子中现在会被拒绝的组合。
  - [ak101-smg-wwii-unique-gp25.json](builds/ak101-smg-wwii-unique-gp25.json) 未按当前规则复核。
- 六份样例保留采集时的 `source.hash_format` 与旧版 sorted-key `content_sha256`，不能与 schema 3 的保序摘要直接比较，也不能仅据差异判定数据变化。

### 获取性与兼容层加入前的验证

- `uv run python -m unittest discover -s tests -v`：65 项全部通过，含小规模穷举 oracle 对照、各目标、容量与下挂组合、深层反向冲突、大小写冲突键的顺序覆盖与 self-conflict、负数基础人机工效、人机封顶、小数细差、转轮弹巢、有符号与零基础后坐力、保序快照及非法 session 零 HTTP 请求等回归。目录链接逃逸、`.tmp` 根链接到工作区内生产目录或工作区外目录的回归在本机均实际执行，未跳过。
- CLI 实测：`--help`、以名称代替模板 ID 时返回候选、`NaN` 权重报错、极短时限返回 `unknown` 与空 `solutions`。
- 向已存在的快照路径再次执行 `snapshot`：退出码 1、`data_error`（File exists），原文件前后 SHA-256 相同。
- `snapshot` 实际读取运行时数据，Decimal 版本为 7084 个物品模板、39805 条英文本地化，`weapons --query AK-12` 查询成功。
- 之后的验证仅使用已有完整快照离线进行：先核对旧摘要，再明确转换为 schema 2，保留旧来源元数据而不伪造新的 GET。当时 6 份示例的 9 个 build 均通过大小写冲突键规则校验，原已证明且仍合法的最优解保留原证明，原未证明项不升级。
- 非法 session 的 CLI smoke 指向非生产端口 `https://127.0.0.1:1`：退出码 2、`invalid_arguments`，API 与 CLI 回归均确认 HTTP 方法从未被调用。
- schema 2 离线 CLI smoke：含非标准大小写冲突键的隔离 fixture 选择合法的 `safe` 配件而非冲突的高收益候选。完整快照上的 APB 返回 `optimal`、`Rtotal=0`、有效人机 100、4 件配件。
- 隔离文件系统 smoke：普通 `.tmp` 输出成功。把隔离安装的 `.tmp` 根链接到其模拟生产目录后，保存被拒绝且目标文件不存在，真实 `.tmp` 根与生产目录未被改动。

下表是 `examples/` 中六份输出的实际结果。请求时限未记录在 JSON 中，耗时取 `search.elapsed_seconds`，AK-12 各次为 23.7–50.1 s。

|#|样例|请求|结果|
|---|---|---|---|
|1|[ak12-recoil.json](examples/ak12-recoil.json)|recoil，容量 ≥ 30，前 3|45.0 s 返回 2 个，顶层 `feasible`，`ranking_proven: true`。#1：ΣRecoil −85%，垂直 15，水平 45.3，人机 65，30 发，28 件，主目标、并列项、最少配件均已证明。#2：属性相同，32 件，并列项未证明。第 3 个在限时内未求出|
|2|[ak12-launcher-ergo.json](examples/ak12-launcher-ergo.json)|ergo，必须下挂，容量 ≥ 30，前 2|39.8 s，`optimal`，两解均全部证明。人机 raw 100.3、有效 100，ΣRecoil −56%（垂直 44，水平 132.88），30 发，31 件|
|3|[ak12-launcher-weighted.json](examples/ak12-launcher-weighted.json)|weighted 1:1，必须下挂，容量 ≥ 30，前 2|23.7 s，`optimal`，两解均全部证明。`score` 165.5，ΣRecoil −76.5%（垂直 23.5，水平 70.97），人机 89，30 发，27 件|
|4|[ak12-pareto.json](examples/ak12-pareto.json)|pareto，容量 ≥ 30，最多 3 点|50.1 s 返回 2 个，顶层 `feasible`。点 1：ΣRecoil −85%（垂直 15），人机 65，`pareto_point`。候选 2：ΣRecoil −84.5%（垂直 15.5），人机 70，主目标已证明但并列项在限时内未证明，标 `remaining_builds`，不算已证明的 Pareto 点|
|5|[ak102-launcher-infeasible.json](examples/ak102-launcher-infeasible.json)|AK-102 Meta，ergo，必须下挂，前 2|0.67 s 证明 `infeasible`，`exhausted: true`。该枪 `mod_launcher` 槽的 Filter 为空，整棵树装不上 Launcher|
|6|[apb-zero-recoil.json](examples/apb-zero-recoil.json)|APB Unique，recoil，容量 ≥ 20，前 1|0.047 s，`optimal`，主目标、并列项、最少配件均已证明。`Rbase` 为 0，后坐力收益与 `Rtotal` 都是 0。人机 raw 168、有效 100，50 发，4 件，`score` 100|

在当前版本按相同参数重跑这六份样例的命令：

```sh
uv run spt-build optimize --snapshot snapshots/ak101-installation-v3.json --weapon 6499849fc93611967b034949 --objective recoil --min-capacity 30 --solutions 3 --time-limit 60 --acquisition templates
uv run spt-build optimize --snapshot snapshots/ak101-installation-v3.json --weapon 6499849fc93611967b034949 --objective ergo --launcher required --min-capacity 30 --solutions 2 --time-limit 60 --acquisition templates
uv run spt-build optimize --snapshot snapshots/ak101-installation-v3.json --weapon 6499849fc93611967b034949 --objective weighted --recoil-weight 1 --ergo-weight 1 --launcher required --min-capacity 30 --solutions 2 --time-limit 60 --acquisition templates
uv run spt-build optimize --snapshot snapshots/ak101-installation-v3.json --weapon 6499849fc93611967b034949 --mode pareto --min-capacity 30 --solutions 3 --time-limit 60 --acquisition templates
uv run spt-build optimize --snapshot snapshots/ak101-installation-v3.json --weapon 692066c72a67c15014e822f1 --objective ergo --launcher required --solutions 2 --time-limit 60 --acquisition templates
uv run spt-build optimize --snapshot snapshots/ak101-installation-v3.json --weapon 6964003370da0d7ae0857060 --objective recoil --min-capacity 20 --solutions 1 --time-limit 60 --acquisition templates
```

- 样例产生时还没有 `--acquisition` 参数，`--acquisition templates` 让当前版本按同样不筛选获取性的范围求解。
- `--time-limit 60` 是建议值，不是当时的请求值。
- 重跑结果可能与表中不同：快照不是产生样例时的那份，兼容层也增加了冲突约束，耗时与限时内能证明到哪一步还随机器负载变化。
