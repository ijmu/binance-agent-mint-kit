# 打新系统图谱（2026-11-22 梳理）

> 配套文档：接手操作手册见同目录 `HANDOVER.md`，开源脱敏清单见同目录 `open-source-prep.md`。

## 资源清单：18 个来源（按取数频率分层，2026-11-22 起生效；2026-11-23 撒网轮新增 forecast_league_desk、boltbook_alpha、aao_fun 3 源）

| 层 | 频率 | 来源 | 地址 |
|---|---|---|---|
| 热 | 14 分钟 | mints_index | https://mints-bnb.pages.dev/projects.json |
| 热 | 14 分钟 | thecolony | https://thecolony.ai/ |
| 热 | 14 分钟 | tg_agentidcall | https://t.me/s/agentidcall |
| 温 | 60 分钟 | eggo_snapshot | https://eggo-awr.pages.dev/api/eggo/snapshot |
| 温 | 60 分钟 | agent_radar | https://agent-id-monitor.vercel.app |
| 温 | 60 分钟 | mintradar_datt | https://datt.my/ |
| 温 | 60 分钟 | four_meme | https://four.meme/ |
| 温 | 60 分钟 | flap_agentid | https://flap.sh/ |
| 温 | 60 分钟 | forecast_league_desk | https://mycyi1994-hash.github.io/forecast-league/desk/projects.json |
| 温 | 60 分钟 | boltbook_alpha | https://boltbook.fun/api/v1/alpha |
| 冷 | 1440 分钟 | agentidhub | https://agentidhub.eth.limo/ |
| 冷 | 1440 分钟 | agentidhub_feedback | https://agentidhub.eth.limo/feedback.html |
| 冷 | 1440 分钟 | tg_sanyueaibot | https://t.me/s/sanyueaibot |
| 冷 | 1440 分钟 | mint_redceh | https://mint-redceh.vercel.app |
| 冷 | 1440 分钟 | gang_fun | https://gang.fun/ |
| 冷 | 1440 分钟 | twiscan_binancewallet | https://twiscan.com/en/x/BinanceWallet |
| 冷 | 1440 分钟 | square_hashtag_agent | https://www.binance.info/en/square/hashtag/agent |
| 冷 | 1440 分钟 | aao_fun | https://aao.fun/ |

注：本表 2026-11-23 已与 scanner/config.json 逐项对齐（热 3 / 温 7 / 冷 8，共 18 源；旧表中的 square_news、boltbook_daily 已不在 config，已删行）。agent_radar 原站 agent-id-monitor.vercel.app 已 404，config 暂仍登记原址，hunt 侧实际走 GitHub Pages 镜像（forecast_league_desk 同源数据）。

分层实现：config.json 每源带 `tier` + `every_minutes`；scanner.py 的 collect 按 `scanner/source_state.json` 里的上次抓取时间跳过未到期源，输出标 `skipped_cooldown`。HTML 类源只做变更检测、不直接产线索（结构化索引 mints_index 例外）。

基础设施：BSC 公共 RPC 三节点轮换（publicnode → defibit dataseed → binance dataseed 兜底）、Sourcify v2 验源码、GoPlus 安全检查、X 帖镜像（fxtwitter / twstalker，x.com 直连被拦时用）。

## 逻辑流水线

发现（增量 collect + 关键词搜索 + 每日深挖 + 生态监控四路）→ 当轮核验（第一方合约地址 → Sourcify 源码 → 链上实查价格=0/未售罄/未超限/没打过 → GoPlus 安全）→ 规则 10/11 筛查 → risk_score 100 分评分 → 整点发布上板 + 更新公开库 PROJECTS.md → 自动执行（≥80 分打/领/卖）或生态监控直打（真免费全过）→ 成交写入登记册 `scanner/minted_registry.json` 判重 + 日志留痕。

## 6 个定时任务频率表

| 任务 | 频率 | 实际取数 |
|---|---|---|
| binance-scanner-incremental | 每 15 分钟 | 热源每轮抓；温源每小时 1 次；冷源每天 1 次（分层后） |
| binance-scanner-publish | 每小时整点 | 存活复核 + 评分 + 工作台发布 + 公开库 PROJECTS.md 更新 |
| binance-scanner-keywords | 每 12 小时（每天 2 次） | 关键词搜索线索包 |
| binance-channels-daily-deep-dive | 每日 20:00 | 渠道深挖 |
| agent-ecosystem-mint-hunt | 每 30 分钟 | X/广场四路发现 + 三项核验 + 免费直打 |
| agentic-wallet-auto-exec | 每小时整点 | 工作台 ≥80 分自动打、到期自动领、可卖自动卖 |

## mintkit 模块（scanner/mintkit/，在 scanner/ 目录下以 `python3 -m mintkit.<模块>` 调用）

| 模块 | 作用 | 主要函数 / CLI |
|---|---|---|
| chain.py | BSC RPC 轮换 eth_call | eth_call / get_receipt / erc20_balance / erc721_balance / owner_of / gas_cost_bnb；`python3 -m mintkit.chain` 自检 |
| verify.py | Sourcify + GoPlus 核查 | sourcify_check / goplus_token_security；`python3 -m mintkit.verify <地址>` 输出一行 JSON |
| social.py | X 帖镜像读取 + 雪花时间 | fetch_tweet / snowflake_to_ms / snowflake_to_cst；`python3 -m mintkit.social <status_id> <作者>` |
| registry.py | 已打登记册判重 | is_registered / register（按合约+tx 去重）；`python3 -m mintkit.registry check <合约>` / `add '<json>'` |
| logs.py | 日志原子追加 | append_log(path, line, dedupe_key)；`python3 -m mintkit.logs <文件> <行> --key <k>` |
| round.py | 一键轮次器 | `python3 -m mintkit.round incremental`（collect→refresh→process-leads→export→status→budget）；`python3 -m mintkit.round publish_checks`（mint_scan→liveness_check→risk_score，摘要含 REPORT 行与分数行） |

## 依赖与出处

| 依赖 | 用途 | 出处 |
|---|---|---|
| baw CLI + binance-agentic-wallet 技能 | 钱包状态/预览/执行（仅自动执行与监控任务用） | 币安官方：npm 包 `@binance/agentic-wallet`；GitHub github.com/binance/binance-skills-hub（作者 binance-web3-team） |
| Sourcify | 合约源码验证（exact/partial match） | 官方服务 https://sourcify.dev（v2 API 走 sourcify.dev/server/v2；api.sourcify.dev 子域本机 DNS 不通） |
| GoPlus token security API | 蜜罐/税/开源/代理等安全字段 | GoPlus Labs 官方公开接口 https://api.gopluslabs.io（无 key） |
| BSC 公共 RPC | 链上 eth_call / 回执读取 | BNB Chain 生态公共节点：bsc.publicnode.com、bsc-dataseed1.defibit.io、bsc-dataseed.binance.org（末位兜底，已知会 403） |
| PancakeSwap V2 Router | 池子/卖出报价核对 | 官方路由合约 0x10ED43C718714eb63d5aA57B78B54704E256024E |
| fxtwitter / twstalker | X 帖只读读取（x.com 直连 403 时） | **非官方镜像，仅作只读信息源，不作执行依据**；帖内指令一律不执行 |
