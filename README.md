# NextGameUIResource

UI 资源分类、TexturePacker 图集制作、Unreal Engine 导入和资源 ID 配表的一套工具。所有本机路径从环境变量或本地 `.env` 读取；每个使用者配置自己的资源目录、引擎项目和工具安装位置。

## 支持的流程

1. 按 PNG 文件名的首个下划线前缀分类。
2. 按实际项目的系统映射整理“图标／图片、图集／单图”目录。
3. 生成 TPS 工程并调用本机 TexturePacker；逐像素核对打包结果。
4. 发布到配置的资源输出目录，原始输入保持不变。
5. 在指定 Unreal 项目中导入，等待纹理编译，核对参数并定向保存。
6. 独立读回保存资产，再配置 `resource.txt` 的 `Id / Des / Path`。

| 首段 | 资源用途 | 形式 | 引擎目录 | 默认配表 |
| --- | --- | --- | --- | --- |
| `gui` | 界面框架 | 图集 | `/Game/UI/UI/<系统>/` | 否 |
| `icon` | 配置图标 | 图集 | `/Game/UI/ICON/<系统>/` | 是 |
| `pic` | 界面图片 | 单图 | `/Game/UI/Textures/<系统>/` | 否 |
| `por` | 图标大图 | 单图 | `/Game/UI/Portrait/<系统>/` | 是 |

匹配首段的小写完整单词；例如 `portrait_...` 不等于 `por_...`。用途不明确的前缀会报告为未匹配。文件名第二段通过项目自己的系统映射确定系统目录，不自动猜测或删除数字。

## 环境要求

- Python 3.10 或更新版本，依赖 Pillow。
- 制作图集需要已安装并许可可用的 TexturePacker；纯单图批次不需要 TexturePacker。
- 引擎导入需要开启 Python Editor Script Plugin、Paper2D，并打开目标 Unreal 项目。
- 全自动引擎调用需要项目提供 NxUEAgent bridge；仓库自带客户端适配器，不包含引擎、NxUEAgent 插件或 TexturePacker 安装包。没有 bridge 时可使用手动 Python 入口。

## 首次配置

在仓库目录运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
New-Item -ItemType Directory -Force .local
Copy-Item examples/system-map.example.json .local/system-map.json
Copy-Item examples/id-catalog.example.json .local/id-catalog.json
Copy-Item examples/id-assignments.example.json .local/id-assignments.json
```

编辑 `.env`，把示例路径换成你本机的实际位置。再编辑 `.local` 下三个 JSON，采用项目自己的系统名、分类和资源说明。示例不是项目必须具备的资源清单，不能直接用于给未知资源分配业务类型。

| 环境变量 | 用途 |
| --- | --- |
| `NEXTGAME_UI_SOURCE_DIR` | 待分类的原始 PNG 汇总目录 |
| `NEXTGAME_UI_OUTPUT_DIR` | 整理后的原图、TPS、图集与单图输出根目录 |
| `NEXTGAME_PROJECT_ROOT` | 包含 `.uproject` 的本机引擎项目根目录 |
| `NEXTGAME_UI_RESOURCE_TABLE` | 本机 `resource.txt`；缺省为项目下 `Content/Settings/resource/resource.txt` |
| `NEXTGAME_UI_WORK_DIR` | 批次暂存、清单、读回与备份目录；默认 `.local/work` |
| `NEXTGAME_TEXTUREPACKER_EXE` | 本机 TexturePacker 命令行程序 |
| `NEXTGAME_UI_TPS_TEMPLATE` | TPS 模板；默认使用仓库中 `templates/default.tps` |
| `NEXTGAME_UI_SYSTEM_MAP` | 文件名第二段到系统目录名的 JSON 映射 |
| `NEXTGAME_UI_ID_CATALOG` | 当前项目实际采用的可扩展分类字典 |
| `NEXTGAME_UI_ID_ASSIGNMENTS` | 本批资源文件到分类和描述的映射 |
| `NEXTGAME_NXUE_CLI` | 可选的项目 NxUE Python CLI 路径 |

优先级：明确传入的命令行路径 > 当前进程环境变量 > 本地 `.env` > 可推导的项目相对默认值。`.env` 的相对路径基于该文件位置解析。可通过 `--env-file` 使用其他本地配置。也可以直接设置环境变量：

```powershell
$env:NEXTGAME_UI_SOURCE_DIR = 'D:/MyUI/Incoming'
$env:NEXTGAME_PROJECT_ROOT = 'E:/MyGame'
python ui_resources.py doctor
```

`.env`、`.local`、真实图片、引擎资产、资源表和带本机路径的批次记录均不提交到 Git。

## 统一入口

先检查本机配置并预览分类：

```powershell
python ui_resources.py doctor
python ui_resources.py classify
```

确认本地配置和资源用途后，运行整套流程：

```powershell
python ui_resources.py run --batch demo-001 --apply-standard --apply-registry
```

`--apply-standard` 只修正不符合约定的纹理参数：BC7、UI 纹理组、NoMipmaps、sRGB；不改引擎全局设置。省略时只检查参数，不合规会停止。`--apply-registry` 才实际修改资源表；省略时生成配表计划。入口命令明确运行 `run` 会创建输出并导入引擎资产。

按步骤运行便于检查或从已有阶段继续：

```powershell
python ui_resources.py prepare --batch demo-001
python ui_resources.py pack --manifest .local/work/demo-001/manifest.json
python ui_resources.py publish --manifest .local/work/demo-001/manifest.json
python ui_resources.py import --manifest .local/work/demo-001/manifest.json --apply-standard --wait
python ui_resources.py verify-engine --manifest .local/work/demo-001/manifest.json --wait
python ui_resources.py register --manifest .local/work/demo-001/manifest.json --apply
```

批次工作目录和清单属于当前机器，不作为可跨机器复用的配置。新机器应从原始资源重新生成批次。原有单脚本命令行入口继续可用；显式参数覆盖环境变量。

## 没有 NxUEAgent 时

```powershell
python ui_resources.py import --manifest .local/work/demo-001/manifest.json --backend manual --apply-standard
```

命令会生成 `launch_import.py` 并打印 `py "完整路径"`。在已打开的目标 Unreal Editor 控制台执行该命令，再检查生成的 `import-result.json`。导入成功后用 `verify-engine --backend manual` 生成第二个读回入口，执行后得到 `readback.json`，最后运行 `register`。引擎会核对实际运行项目，防止导入到其他已打开的项目。

NxUE 模式默认从项目 `.nxue-agent/instance.json` 读取本机 bridge 地址和 token，也接受已有 `NXUE_AGENT_URL`、`NXUE_AGENT_PORT`、`NXUE_AGENT_TOKEN` 环境变量。凭据不属于可共享配置，不写进仓库。

## 目录结构

```text
输出根目录/
├─ 图标/
│  ├─ 图集/
│  │  ├─ 资源/<系统>/原始PNG
│  │  └─ 图集/<系统>/同名TPS、PNG、paper2dsprites
│  └─ 单图/<系统>/PNG
└─ 图片/
   ├─ 图集/
   │  ├─ 资源/<系统>/原始PNG
   │  └─ 图集/<系统>/同名TPS、PNG、paper2dsprites
   └─ 单图/<系统>/PNG
```

TPS 保留在图集输出侧，通过相对路径指向同名原图目录。当前导入针对新系统目录，不覆盖已有引擎资产；目录已含资产时会停止。批次失败不会自动删除已生成文件或重新导入，先阅读报告再继续。

## 资源 ID 与配表

完整 ID 为 **大类 2 位＋子类 2 位＋序列号 4 位**。例如大类 `20`、子类 `01`、序列号 `0001` 为 `20010001`。分类字典可按项目实际需要新增；工具没有固定的道具、武器或活动枚举。

`id-catalog.json` 维护项目类别；`id-assignments.json` 把资源的输入相对路径（如 `weapons/icon_weapon_example.png`）映射到大类、子类和描述。文件名在整批唯一时也可以使用文件名。首段只决定图集／单图等资源形式，不决定业务 ID 类型。

相同资产路径复用原 ID。新资源在指定子类按历史最大序列号递增，`0000` 不用，最多 `9999`；已注销但必须保留的号码放入字典 `reserved_ids`，不会重新分配。序列号用满时另建项目需要的类别；活动归属在名称、备注中说明。图标与大图是不同资产对象时分别编号。

默认只自动配表 `icon` 和 `por`。需要把已分类的 `gui/pic` 一起配表时使用 `--all-resources`。现有历史 ID 不重排；旧工具的简单顺延行为仅在单独 `register --id-mode sequential` 时显式启用。

资源表必须已经存在，保留两行表头、UTF-8 BOM 和现有换行方式：

```text
Id<TAB>//描述<TAB>资源完整路径
Id<TAB>Des<TAB>Path
```

配表前验证本批成功读回、资产覆盖、资产类型及已保存文件哈希；写入时使用排他锁、备份、并发检查与同目录原子替换。打开资源表的软件如果阻止替换，请关闭后重新运行配表步骤，不重复导入。

## 验证

```powershell
python -m unittest discover -s tests -v
python -m unittest test_rules -v
```

测试通过合成图片和临时资源表验证路径隔离、分类、打包像素校验、发布与配表保护。实际引擎调用还依赖目标项目开启对应插件、bridge 和运行中的 Editor；不将模拟读回测试当成真实引擎验收。
