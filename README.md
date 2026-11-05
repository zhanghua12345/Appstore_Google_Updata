# App Store / Google Play 支付项上传

向项目智能体提供文字、表格或图片，按 [AGENTS.md](./AGENTS.md) 执行：

1. 新任务先运行 `status`、`inventory`，再运行 `python3 -B workflow.py profile-init --replace`。工具从已跟踪的[空白模板](./平台资料空白模板.json)生成根目录 `平台资料.json`；旧资料备份到项目外，不自动带入。
2. 在 `平台资料.json` 填 App 和需要的平台资料：只做 App Store 就填 Apple 字段和根目录购买页图片文件名，只做 Google Play 就填 Google 字段，两平台都做才填两组。对应的 `.p8` 或服务账号 JSON 放到根目录。运行 `python3 -B workflow.py profile`，直到返回 `ready: true`；首次检查未齐全时停在资料步骤，不进入商品核对和制表。
3. 资料齐全后，智能体调用 `start --input ... --replace` 记录本次商品，自动沿用资料表中的 App 和平台。之后核对完整商品表及候选 ID、确认前缀，再运行 `prepare-excel` 生成待确认 Excel。该阶段不写远端。
4. 展示 Excel 后选择实际提交平台，运行 `choose-submit`；只读检查、最终 Excel 确认和提交前检查齐全后，才按授权平台上传。

任意节点可暂停；进度在根目录 当前任务.json。已在资料表填写的 App、平台和账号标识不重复提问。需要用户确认商品、前缀或最终 Excel 时优先使用对话弹框，提供“稍后填写”。资料缺项通过 `平台资料.json` 集中补齐；只填一个平台时另一个平台不阻塞。本机资料页仍可运行 `python3 -B workflow.py serve` 后打开 http://127.0.0.1:8790，作为备用输入；Google 原网页保持单平台上传用途。

新任务不自动读取 registry.json 历史缓存；只有用户明确要求复用时，才按已选 App 展示历史建议供确认。改 App 或明确清缓存会从头核对，不继承旧平台目标。密钥只检测文件，不回传正文、不存浏览器 localStorage。两平台资料区用蓝色高亮说明：**凭据仅在当前设备保存，以便下次确认复用；仅用于连接 Apple/Google 官方服务，不写入 Excel、提交报告或 Git。**这不是“完全不保存”的承诺。

Apple 默认写入待提交状态、不送审；Google 按要求写入/激活，商品激活不等于 App 版本送审。失败先查远端，最多再试一次，最后报告新增、已有、改价、失败和异常。

| 平台 | 材料 |
| --- | --- |
| App Store | App 名称、数字 App ID、Bundle ID、Issuer ID、Key ID、App Store Connect API .p8、目标 App 内购管理权限、真实购买页审核截图；协议/税务/银行按 Apple 要求有效 |
| Google Play | Android 包名、服务账号 JSON、启用 Android Publisher API、服务账号具有目标 App 商品/订阅管理权限；支付账号按要求有效。App 版本送审另需版本/轨道 |

当前未接入用户浏览器会话，登录不能替代 Apple API 凭据。Apple 使用 `.p8`；Google 使用服务账号 JSON。文件存在和格式正确仍不能证明目标 App 权限。本次使用的图片从其他本机目录找到时先复制到根目录，保留原文件。凭据副本仅当前用户可读，实际资料 `平台资料.json` 和凭据文件不入 Git；只有全空模板可提交远程。

[demo.xlsx](./demo.xlsx) 是唯一格式模板，三种示例不可上传。[workflow.py](./workflow.py) 实现持久化、缓存确认、平台暂缓/只制表、截图计数、只读 API 预检、Excel 制作/转换校验、指纹授权及提交条件检查；[agent_form.py](./agent_form.py) 与 [agent_form.html](./agent_form.html) 提供本机资料输入界面，绑定127.0.0.1。实际上传由智能体调用现有功能。内置制表支持 demo 的 USA/USD、一次性金币和周订阅，其他币种/周期需扩展或正确使用现有工具，不会静默改价/周期。运行 python3 -B workflow.py --help 或 python3 -B workflow.py status 查看。

AppStore/、GooglePlay/ 日常只读，只存功能。业务表、截图和报告放根目录；apps.json 只映射已选 App，无默认。Google 网页任务与构建产物分别在 ~/.appstore_google_updata/GooglePlay/jobs/ 和 ~/.appstore_google_updata/GooglePlay/dist/。首次使用 Google 功能前，在 GooglePlay/ 执行 `go build -o gplay .` 构建本机程序；随后执行 `npm start` 启动单平台网页。
