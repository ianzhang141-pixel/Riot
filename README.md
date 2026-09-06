# LOL PC 决策实验室 · 数据工具箱（lolab）

配合本地网站「LOL PC 决策实验室 / Riot API V0.1」使用的命令行工具箱。

只做一件事：**先把真实数据验证清楚，再谈胜率模型。**
本仓库内**不存在**任何假装已经训练好的 V(s) / Q(s,a) 模型。

- 只用 Python 标准库，Mac 自带 `python3` 就能跑，不需要 pip 安装
- 不修改现有项目，只读取项目的 `data/` 目录（JSON 与 SQLite 都会扫）
- 控制台跑在 8010 端口，只监听 127.0.0.1，和现有的 8000 网站互不影响
- API Key 只写进本机 `data/riot_secret.json`（权限 600），页面上只显示遮蔽形式

## 快速开始

**零基础分步操作指南（推荐先看这个）：[docs/使用说明.md](docs/使用说明.md)**

只需要一条命令，其余全部在浏览器里点：

```bash
python3 -m lolab serve      # 打开 http://127.0.0.1:8010
```

网页控制台上可以：选数据目录（支持自动查找）、填 / 验证 Riot API Key、
按 Riot ID 同步最近 N 场（**Match 和 Timeline 两个接口都抓**）、
单场补下载 Timeline、逐分钟核对 State。

也可以全部用终端：

```bash
python3 -m lolab where  --data "你的项目/data"        # 认的是哪个 data 目录
python3 -m lolab check  KR_8368663855                # Timeline 完整性检查
python3 -m lolab fetch  KR_8368663855                # 从 Riot API 补下载
python3 -m lolab items                               # 更新装备价格表
python3 -m lolab state  KR_8368663855 --minute 10    # Timeline → 分钟级 State
python3 -m lolab huya   "虎牙录像地址" --workers 3     # 录像下载（批量 / 并发）
```

## 模块

| 文件 | 作用 |
| --- | --- |
| `paths.py` | 定位项目的 `data/` 目录 |
| `store.py` | 在 `data/` 里扫描 / 保存 Match 与 Timeline（不假设目录结构） |
| `check.py` | Timeline 数据完整性检查，输出中文报告 |
| `riot.py` | Riot API 最小客户端（Key 只从本机读，绝不打印） |
| `ddragon.py` | Data Dragon 装备价格表，用于计算装备价值 |
| `state.py` | **Timeline → 分钟级 State**，严格无未来信息泄漏 |
| `server.py` + `web/console.html` | 网页控制台：数据目录 / API Key / 同步 / 补下载 |
| `web/debug.html` | Timeline / State 逐分钟调试页（含小地图） |
| `huya.py` | 虎牙录像 URL → m3u8 → 最高画质 → FFmpeg → MP4 |

## 关于「无未来信息泄漏」

Timeline 里第 i 帧的 `events` 是第 i-1 帧到第 i 帧之间发生的事。
`state.build()` 因此严格按「先累加本帧事件，再拍下本帧快照」的顺序处理，
任何一帧的 State 只含该时刻及之前可观测的信息。

唯一的例外是 `meta.blueWin` —— 它是将来训练 V(s) 的**标签**，
不属于 State 特征，构造特征时必须排除。

## 还没做的部分

V(s) 胜率模型、胜率曲线、行为抽象、Q(s,a)、Regret 都**尚未实现**。
先完成数据核对，再开始建模。
