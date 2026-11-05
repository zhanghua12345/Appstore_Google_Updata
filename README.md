# App Store / Google Play 支付项上传

向项目智能体提供文字、表格或图片，按 [AGENTS.md](./AGENTS.md) 执行：

1. 新增商品请求按本次新任务处理。先问并记录本次 App 名称，再将文字、截图或 Excel 的所有商品逐行列成表，展示类别、完整 ID、一次性价、首周价、续订价；未给 ID 时按 demo 商品规则加小写 App 名称和英文句点 `.` 展示候选，其他缺失/不适用填 -。商品表后核对商品并给出平台选项，不抽样。
2. 本次 App 名称在商品表前确认，不默认上一次 App。
3. 确认 App Store、Google Play 或两个平台。
4. 选择是否加前缀并输入；dinreel 自动补为 dinreel.，已有点不重复加。
5. 检查根目录审核截图；可选“稍后填写”，继续填写后续资料，Apple 仍是本次目标。
6. 本次执行安排，可留空：全部处理、先做 Excel、先提交某个平台。
7. App Store 对话弹框一次收集 Issuer ID、Key ID、数字 App ID、可选 Bundle ID；检测根目录 .p8，缺失时禁用资料页选择框；每项可选“稍后填写”。
8. Google Play 对话弹框收集包名，检测根目录服务账号 JSON；可选“稍后填写”。仅显示本次选定的平台。
9. 最终检查一次列出全部缺项；齐全后生成根目录 Excel。看到本次文件后说“我已经确认”或“继续”，再次在线核验本次实际提交平台后执行。

任意节点可暂停；进度在根目录 当前任务.json。从用户第一句话开始用 `status` 和 `inventory` 核对本次输入及根目录文件；所有待回答资料项都能选择“稍后填写”，最后用 `check-intake` 一次列全缺项。需要确认或修改意见时，智能体优先弹出交互选择，账号标识一次给出多个输入问题；“稍后填写”始终是自由输入框前的最后一个选项。修改前面字段会让后续确认失效。本机资料页仍可运行 `python3 -B workflow.py serve` 后打开 http://127.0.0.1:8790，用于根目录文件检测和备用输入；无 `.p8` 时选择框禁用，有文件时高亮。Google 原网页保持单平台上传用途。

新任务不自动读取 registry.json 历史缓存；只有用户明确要求复用时，才按已选 App 展示历史建议供确认。改 App 或明确清缓存会从头核对，不继承旧平台目标。密钥只检测文件，不回传正文、不存浏览器 localStorage。两平台资料区用蓝色高亮说明：**凭据仅在当前设备保存，以便下次确认复用；仅用于连接 Apple/Google 官方服务，不写入 Excel、提交报告或 Git。**这不是“完全不保存”的承诺。

Apple 默认写入待提交状态、不送审；Google 按要求写入/激活，商品激活不等于 App 版本送审。失败先查远端，最多再试一次，最后报告新增、已有、改价、失败和异常。

| 平台 | 材料 |
| --- | --- |
| App Store | App 名称、数字 App ID（或 Bundle ID 供查找）、Issuer ID、Key ID、App Store Connect API .p8、目标 App 内购管理权限、根目录购买页审核截图；协议/税务/银行按 Apple 要求有效 |
| Google Play | Android 包名、服务账号 JSON、启用 Android Publisher API、服务账号具有目标 App 商品/订阅管理权限；支付账号按要求有效。App 版本送审另需版本/轨道 |

当前未接入用户浏览器会话，登录不能替代 Apple API 凭据。Google 使用 JSON，不是 p8。已有凭据先验证复用；本次使用的文件从其他本机目录找到时先复制到根目录，保留原文件。凭据副本仅当前用户可读，根目录 JSON/.p8 不入 Git，状态只存路径。

[demo.xlsx](./demo.xlsx) 是唯一格式模板，三种示例不可上传。[workflow.py](./workflow.py) 实现持久化、缓存确认、平台暂缓/只制表、截图计数、只读 API 预检、Excel 制作/转换校验、指纹授权及提交条件检查；[agent_form.py](./agent_form.py) 与 [agent_form.html](./agent_form.html) 提供本机资料输入界面，绑定127.0.0.1。实际上传由智能体调用现有功能。内置制表支持 demo 的 USA/USD、一次性金币和周订阅，其他币种/周期需扩展或正确使用现有工具，不会静默改价/周期。运行 python3 -B workflow.py --help 或 python3 -B workflow.py status 查看。

AppStore/、GooglePlay/ 日常只读，只存功能。业务表、截图和报告放根目录；apps.json 只映射已选 App，无默认。Google 网页任务与构建产物分别在 ~/.appstore_google_updata/GooglePlay/jobs/ 和 ~/.appstore_google_updata/GooglePlay/dist/。首次使用 Google 功能前，在 GooglePlay/ 执行 `go build -o gplay .` 构建本机程序；随后执行 `npm start` 启动单平台网页。
