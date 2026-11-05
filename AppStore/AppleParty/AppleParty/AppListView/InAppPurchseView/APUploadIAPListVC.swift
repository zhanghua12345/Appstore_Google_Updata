//
//  InputTableListVC.swift
//  AppleParty
//
//  Created by 易承 on 2020/12/15.
//

import Cocoa

class APUploadIAPListVC: NSViewController {

    @IBOutlet weak var tableView: NSTableView!
    @IBOutlet weak var enterBtn: NSButton!
    @IBOutlet weak var preserveCurrentPriceBtn: NSButton!
    @IBOutlet weak var showApiRateLimitLogsBtn: NSButton!
    @IBOutlet weak var submitSubscriptionReviewBtn: NSButton!

    public var currentApp: App? {
        didSet {
            setupUI()
        }
    }
    public var iaps = [IAPProduct]() {
        didSet {
            self.tableView.reloadData()
        }
    }

    private var screenshotPaths = [String: String]()
    private var uploadFailureSummaries: [String: UploadFailureSummary] = [:]
    private var uploadFailureOrder: [String] = []

    private struct UploadFailureSummary {
        let productId: String
        let productName: String
        let productType: String
        var reasons: [String]
    }

    private struct IntroductoryOfferTarget {
        let territoryId: String
        let pricePointId: String?
        let customerPrice: String
    }

    private final class SubscriptionUploadCatalog {
        var groups: [ASCSubscriptionGroup]
        var subscriptionsByGroupId = [String: [ASCSubscription]]()

        init(groups: [ASCSubscriptionGroup]) {
            self.groups = groups
        }
    }

    override func viewDidLoad() {
        super.viewDidLoad()

        self.tableView.columnAutoresizingStyle = .uniformColumnAutoresizingStyle
        self.tableView.selectionHighlightStyle = .none
        self.tableView.sizeToFit()
    }

    func setupUI() {
        self.view.window?.title = "批量内购买项目上传 - " + (currentApp?.appName ?? "")
        self.tableView.reloadData()
    }

    func showUploadView() {
        // 不能同时 present 出来
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) {
            let mainStoryboard = NSStoryboard(
                name: "InAppPurchseView", bundle: Bundle(for: self.classForCoder))
            let upVC =
                mainStoryboard.instantiateController(withIdentifier: "IAPUploadVCID")
                as? IAPUploadImageVC
            let screenshot = self.iaps.filter({ $0.reviewScreenshot.count > 0 }).map({
                $0.reviewScreenshot
            })
            // 去重后的图片名
            let uniquedshot = screenshot.enumerated().filter { (index, value) -> Bool in
                return screenshot.firstIndex(of: value) == index
            }.map { $0.element }
            upVC?.picnames = uniquedshot
            upVC?.callBackFunc = { paths in
                self.screenshotPaths = paths
                self.tableView.reloadData()
            }
            self.presentAsSheet(upVC!)
        }
    }

    @IBAction func clickedUploadShotBtn(_ sender: Any) {
        showUploadView()
    }

    @IBAction func clickedSPasswordBtn(_ sender: Any) {
        let vc = APASCKeysSettingVC()
        presentAsSheet(vc)
    }

    @IBAction func createIAP(_ sender: Any) {
        let list = self.iaps
        guard list.count > 0 else {
            APHUD.hide(message: "当前 App 无上传的内购商品！", delayTime: 1)
            return
        }

        guard let appid = currentApp?.appId else {
            APHUD.hide(message: "当前 App 的 appleid 为空！", delayTime: 1)
            return
        }

        enterBtn.isEnabled = false
        APHUD.show(message: "上传中", view: self.view)
        uploadFailureSummaries.removeAll()
        uploadFailureOrder.removeAll()
        let uploadIAPs: ((AppStoreConnectKey) -> Void) = { [weak self] ascKey in
            // 上传数据
            self?.updateInAppPurchse(iaps: list, appId: appid, ascKey: ascKey)
        }

        guard let ascKey = InfoCenter.shared.currentASCKey else {
            let vc = APASCKeysSettingVC()
            vc.updateCompletion = { password in
                if let ascKey = password {
                    uploadIAPs(ascKey)
                }
            }
            presentAsSheet(vc)
            return
        }

        uploadIAPs(ascKey)
    }

}

// MARK: - 网络请求
extension APUploadIAPListVC {

    func updateInAppPurchse(iaps: [IAPProduct], appId: String, ascKey: AppStoreConnectKey) {
        let showApiRateLimit = showApiRateLimitLogsBtn.state.rawValue == 1
        let ascAPI = APASCAPI.init(
            issuerID: ascKey.issuerID,
            privateKeyID: ascKey.privateKeyID,
            privateKey: ascKey.privateKey,
            showApiRateLimit: showApiRateLimit)
        ascAPI.addMessage("密钥信息：Issuer ID \(ascKey.issuerID)，Key ID \(ascKey.privateKeyID)")

        Task {
            // 1、获取当前账号下 app，判断是否包含当前 提交商品的 app
            guard let apps = await ascAPI.apps() else {
                self.enterBtn.isEnabled = true
                APHUD.hide()
                APHUD.hide(message: "当前请求异常，请检查密钥是否正确~", delayTime: 2)
                return
            }
            let app = apps.filter { $0.id == appId }
            guard app.count > 0 else {
                self.enterBtn.isEnabled = true
                APHUD.hide()
                APHUD.hide(message: "当前的密钥没有查到App: \(appId)，请检查~", delayTime: 2)
                return
            }

            // 2. 同步显示进度日志
            let sb = NSStoryboard(name: "APDebugVC", bundle: Bundle(for: self.classForCoder))
            let newWC = sb.instantiateController(withIdentifier: "APDebugWC") as? NSWindowController
            newWC?.window?.title = "内购批量上传日志"
            let logVC = newWC?.contentViewController as? APDebugVC
            logVC?.debugLog = "开始上传"
            newWC?.showWindow(self)
            ascAPI.updateMsg = { messages in
                logVC?.debugLog = messages.joined(separator: "\n")
            }

            ascAPI.addMessage("开始处理内购商品，获取现有商品中...")
            // 3、获取所有的内购商品，如果存在的商品就直接修改，如果不存在就创建
            let oldIAPs = await ascAPI.fetchInAppPurchasesList(appId: appId)

            var subscriptionCatalog: SubscriptionUploadCatalog?
            if iaps.contains(where: { $0.inAppPurchaseType == .AUTO_RENEWABLE }) {
                guard let groups = await ascAPI.fetchSubscriptionGroups(appId: appId) else {
                    self.enterBtn.isEnabled = true
                    APHUD.hide()
                    ascAPI.addMessage("获取订阅组失败，已停止订阅商品上传，请稍后重试。❌")
                    return
                }
                subscriptionCatalog = SubscriptionUploadCatalog(groups: groups)
            }

            let submitSubscriptionReview = self.submitSubscriptionReviewBtn.state == .on

            // 4、遍历所有要上传的商品
            for product in iaps {
                // 订阅类型与非订单类型不一样的处理逻辑
                if product.inAppPurchaseType == .AUTO_RENEWABLE {
                    guard let subscriptionCatalog else {
                        self.recordUploadFailure(product: product, reason: "订阅目录获取失败")
                        continue
                    }
                    await createRenewSubscription(
                        appId: appId,
                        product: product,
                        ascAPI: ascAPI,
                        catalog: subscriptionCatalog,
                        submitForReview: submitSubscriptionReview)
                } else {
                    await createInAppPurchase(
                        appId: appId, product: product, oldIAPs: oldIAPs, ascAPI: ascAPI)
                }
            }

            self.outputUploadFailureSummary(ascAPI: ascAPI)
            self.enterBtn.isEnabled = true
            APHUD.hide()
            ascAPI.addMessage("完成全部内购商品，可稍后在苹果后台查看！✅✅✅")
        }
    }

    func recordUploadFailure(product: IAPProduct, reason: String) {
        let key = product.productId
        let typeName = product.inAppPurchaseType.CNValue()
        if uploadFailureSummaries[key] == nil {
            uploadFailureSummaries[key] = UploadFailureSummary(
                productId: product.productId,
                productName: product.name,
                productType: typeName,
                reasons: []
            )
            uploadFailureOrder.append(key)
        }

        guard var summary = uploadFailureSummaries[key] else { return }
        if summary.reasons.contains(reason) == false {
            summary.reasons.append(reason)
        }
        uploadFailureSummaries[key] = summary
    }

    func outputUploadFailureSummary(ascAPI: APASCAPI) {
        guard uploadFailureOrder.isNotEmpty else { return }

        ascAPI.addMessage("本次上传失败的支付项如下：")
        for key in uploadFailureOrder {
            guard let summary = uploadFailureSummaries[key] else { continue }
            let reasonText = summary.reasons.joined(separator: "；")
            ascAPI.addMessage(
                "支付项失败：\(summary.productId)｜\(summary.productName)｜\(summary.productType)｜\(reasonText)")
        }
    }

    // MARK: - 上传内购类型商品

    /// 创建内购商品
    func createInAppPurchase(
        appId: String, product: IAPProduct, oldIAPs: [ASCInAppPurchaseV2], ascAPI: APASCAPI
    ) async {
        ascAPI.addMessage("开始上传内购商品：\(product.productId)，\(product.name) ")
        // 检查是否已经存在此商品，如果存在就修改信息，如果不存在就创建
        let iaps = oldIAPs.filter({ $0.attributes?.productID == product.productId })
        if let iap = iaps.first {
            ascAPI.addMessage("内购已经存在：\(product.productId) ，开始更新信息中...")
            // 0. 审核备注如果原来有值，而新字段无值，则使用原值
            var product = product
            if let note = iap.attributes?.reviewNote, product.reviewNote.isEmpty {
                product.reviewNote = note
            }
            // 1.修改原商品信息
            guard let iap = await ascAPI.updateInAppPurchases(iapId: iap.id, product: product)
            else {
                // 修改失败
                ascAPI.addMessage("内购已经存在：\(product.productId) ，更新信息失败！❌ ")
                return
            }

            // 3. 商品本地化语言
            ascAPI.addMessage("开始更新内购本地化版本：\(product.productId)")
            let localizations = await ascAPI.fetchInAppPurchasesLocalizations(iapId: iap.id)
            for localization in product.localizations {

                // 如果已经存在本地化语言，则更新
                if let locale = localizations.filter({
                    $0.attributes?.locale == localization.locale
                }).first {
                    // 更新
                    ascAPI.addMessage("内购已存在本地化版本：\(localization.locale)，开始更新信息中...")
                    if (await ascAPI.updateInAppPurchasesLocalization(
                        iapLocaleId: locale.id, localization: localization)) != nil
                    {
                        // 本地化语言更新成功
                        ascAPI.addMessage("内购本地化版本：\(localization.locale) ，更新语言成功！✅ ")
                    } else {
                        // 本地化语言更新失败
                        ascAPI.addMessage("内购本地化版本：\(localization.locale) ，更新语言失败！❌ ")
                        recordUploadFailure(
                            product: product,
                            reason: "本地化更新失败：\(localization.locale)")
                    }
                } else {
                    // 创建
                    await createIAPLocalization(
                        iapId: iap.id, localization: localization, product: product, ascAPI: ascAPI)
                }
            }

            // 4. 商品截图
            await createIAPScreenshot(iapId: iap.id, product: product, ascAPI: ascAPI)

            // 5. 销售国家或地区
            await updateIAPAvailableTerritories(iapId: iap.id, product: product, ascAPI: ascAPI)

            // 2. 商品价格档位
            await updateIAPPricePoint(iapId: iap.id, product: product, ascAPI: ascAPI)

        } else {
            // 1. 创建新的商品
            guard let iap = await ascAPI.createInAppPurchases(appId: appId, product: product) else {
                // 创建失败
                ascAPI.addMessage("内购商品：\(product.productId) ，创建失败！❌ ")
                recordUploadFailure(product: product, reason: "内购项创建失败")
                return
            }

            // 3. 商品本地化语言
            for localization in product.localizations {
                await createIAPLocalization(
                    iapId: iap.id, localization: localization, product: product, ascAPI: ascAPI)
            }

            // 4. 商品截图
            await createIAPScreenshot(iapId: iap.id, product: product, ascAPI: ascAPI)

            // 5. 销售国家或地区
            await updateIAPAvailableTerritories(iapId: iap.id, product: product, ascAPI: ascAPI)

            // 2. 商品价格档位
            await updateIAPPricePoint(iapId: iap.id, product: product, ascAPI: ascAPI)
        }

        ascAPI.addMessage("内购商品：\(product.productId)，\(product.name) ，上传完成！\n")
    }

    /// 创建内购商品价格档位
    func updateIAPPricePoint(iapId: String, product: IAPProduct, ascAPI: APASCAPI) async {
        guard let schedule = product.priceSchedules else {
            ascAPI.addMessage("无价格计划表：\(product.productId) ，请确认！❌ ")
            recordUploadFailure(product: product, reason: "价格计划表不存在")
            return
        }

        let baseTerritory = schedule.baseTerritory
        let baseCustomerPrice = schedule.baseCustomerPrice.normalizePrice()

        ascAPI.addMessage("开始更新价格计划表：\(product.productId)，\(baseTerritory)，\(baseCustomerPrice) \n")

        let points = await ascAPI.fetchPricePoints(iapId: iapId, territory: [baseTerritory])
        if let point = points.filter({
            $0.attributes?.customerPrice!.normalizePrice() == baseCustomerPrice
        }).first {
            var manualPrices: [Any] = []
            var included: [Any] = []

            ascAPI.addMessage("开始构建基准国家和自定价格：")
            // base Territory
            manualPrices.append([
                "id": "${\(baseTerritory)-\(included.count)}", "type": "inAppPurchasePrices",
            ])
            included.append(
                ascAPI.fetchInAppPurchasePriceSchedule(
                    scheduleId: baseTerritory, pricePointId: point.id, iapId: iapId,
                    index: included.count))

            // customerPrice
            for pricePoint in schedule.manualPrices {
                let territory = pricePoint.territory
                let customerPrice = pricePoint.customerPrice.normalizePrice()
                let points = await ascAPI.fetchPricePoints(iapId: iapId, territory: [territory])
                if let point = points.filter({
                    $0.attributes?.customerPrice!.normalizePrice() == customerPrice
                }).first {
                    manualPrices.append([
                        "id": "${\(territory)-\(included.count)}", "type": "inAppPurchasePrices",
                    ])
                    included.append(
                        ascAPI.fetchInAppPurchasePriceSchedule(
                            scheduleId: territory, pricePointId: point.id, iapId: iapId,
                            index: included.count))
                } else {
                    ascAPI.addMessage("自定价格的内购价格点：\(territory)，\(customerPrice) ，未找到此档位！❌ ")
                    recordUploadFailure(
                        product: product,
                        reason: "自定价格未找到价格点：\(territory) / \(customerPrice)")
                }
            }

            ascAPI.saveLogs(log: "内购的基准国家和自定价格：\(manualPrices)，\(included)")

            if (await ascAPI.updateInAppPurchasePricePoint(
                iapId: iapId, baseTerritoryId: baseTerritory, manualPrices: manualPrices,
                included: included)) != nil
            {
                // 价格档位配置成功
                ascAPI.addMessage("内购价格点：\(baseTerritory)，\(baseCustomerPrice) ，更新价格成功！✅ ")
            } else {
                // 价格档位配置失败
                ascAPI.addMessage("内购价格点：\(baseTerritory)，\(baseCustomerPrice) ，更新价格失败！❌ ")
                recordUploadFailure(
                    product: product,
                    reason: "价格档位配置失败：\(baseTerritory) / \(baseCustomerPrice)")
            }
        } else {
            // 找不到价格档位
            ascAPI.addMessage("基准国家的内购价格点：\(baseTerritory)，\(baseCustomerPrice) ，未找到此档位！❌ ")
            recordUploadFailure(
                product: product,
                reason: "基准国家价格点未找到：\(baseTerritory) / \(baseCustomerPrice)")
        }
    }

    /// 创建内购商品本地化信息
    func createIAPLocalization(
        iapId: String, localization: IAPLocalization, product: IAPProduct, ascAPI: APASCAPI
    ) async {
        ascAPI.addMessage("开始更新本地化版本：\(product.productId)，\(localization.locale)")
        if (await ascAPI.createInAppPurchasesLocalization(iapId: iapId, localization: localization))
            != nil
        {
            // 本地化语言配置成功
            ascAPI.addMessage("内购本地化版本：\(localization.locale) ，更新语言成功！✅ ")
        } else {
            // 本地化语言配置失败
            ascAPI.addMessage("内购本地化版本：\(localization.locale) ，更新语言失败！❌ ")
            recordUploadFailure(
                product: product,
                reason: "本地化创建失败：\(localization.locale)")
        }
    }

    /// 更新内购商品的送审截图
    func createIAPScreenshot(iapId: String, product: IAPProduct, ascAPI: APASCAPI) async {
        ascAPI.addMessage("开始更新内购商品的送审截图：\(product.productId)，\(product.reviewScreenshot)")
        let imgName = product.reviewScreenshot
        guard let imgPath = screenshotPaths[imgName] else {
            ascAPI.addMessage("内购商品：\(product.productId) 无送审截图或未上传截图~")
            recordUploadFailure(product: product, reason: "未上传送审截图")
            return
        }

        let imaUrl = URL.init(fileURLWithPath: imgPath)
        let uploadFileName = imaUrl.lastPathComponent.isEmpty ? imgName : imaUrl.lastPathComponent
        guard let fileMD5 = URL.init(fileURLWithPath: imgPath).fileMD5() else {
            ascAPI.addMessage("内购商品截图文件错误：\(imgPath) ，无法生成 md5 值~")
            recordUploadFailure(product: product, reason: "截图文件无法生成 MD5")
            return
        }

        let oldShot = await ascAPI.fetchInAppPurchasesScreenshot(iapId: iapId)
        // 存在需要删除，避免文件名不一样或者过期文件
        if let ost = oldShot {
            ascAPI.addMessage("删除旧的送审截图：\(ost.attributes?.fileName ?? "")")
            let status = await ascAPI.deleteInAppPurchasesScreenshot(iapShotId: ost.id)
            if status != 204 {
                ascAPI.addMessage("内购商品截图创建失败：\(imgName) ，无法删除旧截图~")
                recordUploadFailure(product: product, reason: "送审截图删除旧图失败")
            }
        }

        ascAPI.addMessage("创建新的送审截图：\(uploadFileName)")
        // 创建截图
        let imaSize = imaUrl.fileSizeInt()
        guard imaSize > 0 else {
            ascAPI.addMessage("内购商品截图文件错误：\(imgPath) ，文件大小为 0~")
            recordUploadFailure(product: product, reason: "截图文件大小为 0")
            return
        }
        guard
            let shot = await ascAPI.createInAppPurchasesScreenshot(
                iapId: iapId, fileName: uploadFileName, fileSize: imaSize)
        else {
            // 创建失败
            ascAPI.addMessage("内购商品：\(product.productId) ，创建送审截图失败！❌ ")
            recordUploadFailure(product: product, reason: "送审截图创建失败")
            return
        }

        // 根据苹果接口返回的上传接口上传
        guard let method = shot.attributes?.uploadOperations?.first?.method,
            let url = shot.attributes?.uploadOperations?.first?.url,
            let requestHeaders = shot.attributes?.uploadOperations?.first?.requestHeaders,
            let baseURL = URL(string: url)
        else {
            ascAPI.addMessage("内购商品：\(product.productId) ，创建送审截图失败！苹果参数异常~ ❌ ")
            recordUploadFailure(product: product, reason: "送审截图上传参数异常")
            return
        }

        var request = URLRequest(url: baseURL)
        request.httpMethod = method
        for header in requestHeaders {
            request.headers[header.name ?? ""] = header.value ?? ""
        }

        ascAPI.addMessage("上传新的送审截图：\(uploadFileName)")
        // 上传图片
        guard let response = try? await URLSession.shared.upload(for: request, fromFile: imaUrl)
        else {
            ascAPI.addMessage("内购商品：\(product.productId) ，创建送审截图失败！上传图片异常~ ❌ ")
            recordUploadFailure(product: product, reason: "送审截图上传异常")
            return
        }
        guard let responseCode = (response.1 as? HTTPURLResponse)?.statusCode, responseCode == 200
        else {
            ascAPI.addMessage(
                "内购商品：\(product.productId) ，创建送审截图失败！上传图片异常 \(response.1.description)~ ❌ ")
            recordUploadFailure(product: product, reason: "送审截图上传响应异常")
            return
        }

        ascAPI.addMessage("提交新的送审截图：\(uploadFileName)")
        // 确认图片
        if (await ascAPI.updateInAppPurchasesScreenshot(iapShotId: shot.id, fileMD5: fileMD5))
            != nil
        {
            ascAPI.addMessage("内购商品：\(product.productId) ，送审截图上传成功！✅ ")
        } else {
            ascAPI.addMessage("内购商品：\(product.productId) ，送审截图可能上传失败！ ")
            recordUploadFailure(product: product, reason: "送审截图确认失败")
        }
    }

    /// 销售国家或地区
    func updateIAPAvailableTerritories(iapId: String, product: IAPProduct, ascAPI: APASCAPI) async {
        let inAll = product.territories.availableInAllTerritories
        let inNew = product.territories.availableInNewTerritories
        let summary = territoryInfo(product: product)
        let newTerritory = inNew ? "将来新国家(地区)时自动提供！" : "将来新国家(地区)时不自动提供!"
        ascAPI.addMessage("开始更新内购商品的销售国家/地区：\(summary)")

        guard !inAll else {
            var allTerritories: [[String: String]] = []
            if let territories = await ascAPI.territories() {
                territories.forEach { territory in
                    allTerritories.append([
                        "type": "territories",
                        "id": territory.id,
                    ])
                }
                // 更新全部国家地区
                if (await ascAPI.updateInAppPurchasesAvailabilityTerritories(
                    iapId: iapId, availableTerritories: allTerritories,
                    availableInNewTerritories: inNew)) != nil
                {
                    ascAPI.addMessage("选择：所有国家(地区)销售，\(newTerritory)，更新成功！✅ ")
                } else {
                    ascAPI.addMessage("选择：所有国家(地区)销售，\(newTerritory)，更新失败！❌ ")
                    recordUploadFailure(product: product, reason: "销售国家/地区更新失败")
                }
            } else {
                ascAPI.addMessage("选择：所有国家(地区)销售，\(newTerritory)，无法设置！获取国家标识码失败！❌ ")
                recordUploadFailure(product: product, reason: "销售国家/地区列表获取失败")
            }
            return
        }

        /// 选择销售的国家或地区
        var territories: [[String: String]] = []
        product.territories.territories?.forEach({ territory in
            territories.append([
                "type": "territories",
                "id": territory.id,
            ])
        })

        let customerTerritory =
            product.territories.territories?.map({ $0.id }).joined(separator: ",") ?? "无"
        if (await ascAPI.updateInAppPurchasesAvailabilityTerritories(
            iapId: iapId, availableTerritories: territories, availableInNewTerritories: inNew))
            != nil
        {
            ascAPI.addMessage("内购商品的销售国家/地区：\(customerTerritory) ，更新成功！✅ ")
        } else {
            ascAPI.addMessage("内购商品的销售国家/地区：\(customerTerritory) ，更新失败！❌ ")
            recordUploadFailure(product: product, reason: "销售国家/地区更新失败")
        }
    }

    // MARK: - 上传订阅商品

    /// 订阅商品创建或更新
    private func createRenewSubscription(
        appId: String,
        product: IAPProduct,
        ascAPI: APASCAPI,
        catalog: SubscriptionUploadCatalog,
        submitForReview: Bool
    ) async {
        let groupName = product.groupName
        var currentSubGroup: ASCSubscriptionGroup?
        // 订阅组在上传开始时只拉取一次，避免每个商品重复扫描全部组。
        for subGroup in catalog.groups {
            if subGroup.attributes?.referenceName == groupName {
                currentSubGroup = subGroup
                break
            }
        }

        // 创建订阅组
        if currentSubGroup == nil,
            let group = await ascAPI.createSubscriptionGroups(appId: appId, groupName: groupName)
        {
            currentSubGroup = group
            catalog.groups.append(group)
            catalog.subscriptionsByGroupId[group.id] = []
        } else if currentSubGroup == nil {
            ascAPI.addMessage("订阅商品：\(product.productId) ，创建订阅组失败！❌ ")
            recordUploadFailure(product: product, reason: "订阅组创建失败")
            return
        }

        //订阅组设置国际化
        if let group = currentSubGroup {
            if let localization = product.groupLocalization {
                guard
                    let existingLocalizations =
                        await ascAPI.fetchSubscriptionGroupLocalizations(iapGroupId: group.id)
                else {
                    recordUploadFailure(
                        product: product,
                        reason: "订阅组本地化查询失败：\(localization.locale)")
                    return
                }
                if existingLocalizations.contains(where: {
                    $0.attributes?.locale == localization.locale
                }) {
                    ascAPI.addMessage(
                        "订阅组本地化已经存在：\(groupName)，\(localization.locale)，跳过重复创建 ✅ ")
                } else if await ascAPI.createSubscriptionGroupLocalizations(
                    iapGroupId: group.id, name: localization.name, locale: localization.locale,
                    customAppName: nil) == nil
                {
                    recordUploadFailure(
                        product: product,
                        reason: "订阅组本地化创建失败：\(localization.locale)")
                    return
                }
            }
        }

        // 只获取当前订阅组的商品，同一组后续直接使用缓存。
        guard let currentSubGroup else { return }
        let subscriptions: [ASCSubscription]
        if let cached = catalog.subscriptionsByGroupId[currentSubGroup.id] {
            subscriptions = cached
        } else {
            guard
                let fetched = await ascAPI.fetchSubscriptionGroupSubscriptions(
                    iapGroupId: currentSubGroup.id)
            else {
                recordUploadFailure(product: product, reason: "订阅组商品查询失败")
                return
            }
            catalog.subscriptionsByGroupId[currentSubGroup.id] = fetched
            subscriptions = fetched
        }

        // 3、查看是否存在订阅商品，不存在就创建，存在就更新
        let subs = subscriptions.filter({ $0.attributes?.productID == product.productId })
        if let sub = subs.first {
            if let state = sub.attributes?.state, state != .missingMetadata {
                guard product.introductoryOffer != nil else {
                    ascAPI.addMessage("订阅商品已经存在：\(product.productId)，当前状态：\(state.rawValue)，跳过更新信息...")
                    return
                }

                ascAPI.addMessage(
                    "订阅商品已经存在：\(product.productId)，当前状态：\(state.rawValue)，仅补充介绍性优惠...")
                _ = await updateSubscriptionIntroductoryOffer(
                    iapId: sub.id, product: product, ascAPI: ascAPI)
                return
            }

            ascAPI.addMessage("订阅商品已经存在：\(product.productId) ，开始更新信息中...")
            // 0. 审核备注如果原来有值，而新字段无值，则使用原值
            var product = product
            if let note = sub.attributes?.reviewNote, product.reviewNote.isEmpty {
                product.reviewNote = note
            }
            // 1.修改原商品信息
            guard let iap = await ascAPI.updateSubscription(iapId: sub.id, product: product) else {
                // 修改失败
                ascAPI.addMessage("订阅商品已经存在：\(product.productId) ，更新信息失败！❌ ")
                recordUploadFailure(product: product, reason: "订阅项更新失败")
                return
            }

            // 2. 商品本地化语言
            ascAPI.addMessage("开始更新订阅商品本地化版本：\(product.productId)")
            let localizations = await ascAPI.fetchSubscriptionLocalizations(iapId: iap.id)
            //            for localization in product.localizations {
            if let localization = product.subscriptionLocalization {
                // 如果已经存在本地化语言，则更新
                if let locale = localizations.filter({
                    $0.attributes?.locale == localization.locale
                }).first {
                    // 更新
                    ascAPI.addMessage("订阅商品已存在本地化版本：\(localization.locale)，开始更新信息中...")
                    if (await ascAPI.updateSubscriptionLocalization(
                        iapLocaleId: locale.id, localization: localization)) != nil
                    {
                        // 本地化语言更新成功
                        ascAPI.addMessage("订阅商品本地化版本：\(localization.locale) ，更新语言成功！✅ ")
                    } else {
                        // 本地化语言更新失败
                        ascAPI.addMessage("订阅商品本地化版本：\(localization.locale) ，更新语言失败！❌ ")
                        recordUploadFailure(
                            product: product,
                            reason: "订阅本地化更新失败：\(localization.locale)")
                    }
                } else {
                    // 创建
                    await createSubscriptionLocalization(
                        iapId: iap.id, localization: localization, product: product, ascAPI: ascAPI)
                }
            }

            // 4. 商品截图
            await createSubscriptionScreenshot(iapId: iap.id, product: product, ascAPI: ascAPI)

            // 5. 销售国家或地区
            await updateSubscriptionAvailableTerritories(
                iapId: iap.id, product: product, ascAPI: ascAPI)

            // 3. 商品价格档位
            await updateSubscriptionPricePoint(iapId: iap.id, product: product, ascAPI: ascAPI)

            // Apple 后台偶发会在创建/更新后错误显示“元数据缺失”，补一次等价 PATCH 来触发状态刷新。
            await refreshSubscriptionMetadata(iapId: iap.id, product: product, ascAPI: ascAPI)

            // 6. 首周介绍性优惠必须最后创建，避免后续元数据 PATCH 与已有优惠冲突。
            let introductoryOfferReady = await updateSubscriptionIntroductoryOffer(
                iapId: iap.id, product: product, ascAPI: ascAPI)

            if submitForReview, introductoryOfferReady {
                await submitSubscriptionForReview(iapId: iap.id, product: product, ascAPI: ascAPI)
            } else if submitForReview {
                ascAPI.addMessage("订阅商品：\(product.productId) 首周优惠未配置完成，已跳过提交审核！❌ ")
            }

        } else {
            // 1. 创建新的商品
            let iapGroupId = currentSubGroup.id
            guard let iap = await ascAPI.createSubscription(
                iapGroupId: iapGroupId, product: product)
            else {
                // 创建失败
                ascAPI.addMessage("订阅商品：\(product.productId) ，创建失败！❌ ")
                recordUploadFailure(product: product, reason: "订阅项创建失败")
                return
            }
            catalog.subscriptionsByGroupId[iapGroupId, default: []].append(iap)

            // 2. 商品本地化语言
            if let localization = product.subscriptionLocalization {
                await createSubscriptionLocalization(
                    iapId: iap.id, localization: localization, product: product, ascAPI: ascAPI)
            }
            //            for localization in product.localizations {
            //                await createSubscriptionLocalization(iapId: iap.id, localization: localization, product: product, ascAPI: ascAPI)
            //            }

            // 4. 商品截图
            await createSubscriptionScreenshot(iapId: iap.id, product: product, ascAPI: ascAPI)

            // 5. 销售国家或地区
            await updateSubscriptionAvailableTerritories(
                iapId: iap.id, product: product, ascAPI: ascAPI)

            // 3. 商品价格档位
            await updateSubscriptionPricePoint(iapId: iap.id, product: product, ascAPI: ascAPI)

            // Apple 后台偶发会在创建/更新后错误显示“元数据缺失”，补一次等价 PATCH 来触发状态刷新。
            await refreshSubscriptionMetadata(iapId: iap.id, product: product, ascAPI: ascAPI)

            // 6. 首周介绍性优惠必须最后创建，避免后续元数据 PATCH 与已有优惠冲突。
            let introductoryOfferReady = await updateSubscriptionIntroductoryOffer(
                iapId: iap.id, product: product, ascAPI: ascAPI)

            if submitForReview, introductoryOfferReady {
                await submitSubscriptionForReview(iapId: iap.id, product: product, ascAPI: ascAPI)
            } else if submitForReview {
                ascAPI.addMessage("订阅商品：\(product.productId) 首周优惠未配置完成，已跳过提交审核！❌ ")
            }
        }

        ascAPI.addMessage("订阅商品：\(product.productId)，\(product.name) ，上传完成！\n")
    }

    /// 提交订阅商品至审核
    func submitSubscriptionForReview(iapId: String, product: IAPProduct, ascAPI: APASCAPI) async {
        ascAPI.addMessage("开始提交订阅商品审核：\(product.productId)")

        let maxRetryCount = 12
        for retryIndex in 0..<maxRetryCount {
            if let subscription = await ascAPI.fetchSubscription(iapId: iapId),
               let state = subscription.attributes?.state {
                ascAPI.addMessage("订阅商品：\(product.productId) 当前状态：\(state.rawValue)")

                if state == .waitingForReview || state == .inReview {
                    ascAPI.addMessage("订阅商品：\(product.productId) 已处于审核流程，无需重复提交。✅ ")
                    return
                }

                if state == .readyToSubmit {
                    break
                }
            } else {
                ascAPI.addMessage("订阅商品：\(product.productId) 当前状态获取失败，继续尝试提交审核")
                break
            }

            let waitSeconds: UInt64 = 10
            let remainingCount = maxRetryCount - retryIndex - 1
            if remainingCount > 0 {
                ascAPI.addMessage("订阅商品：\(product.productId) 尚未进入准备提交状态，\(waitSeconds) 秒后重试，剩余 \(remainingCount) 次")
                try? await Task.sleep(nanoseconds: waitSeconds * 1_000_000_000)
            }
        }

        if let submission = await ascAPI.submitSubscriptionForReview(iapId: iapId) {
            ascAPI.addMessage("订阅商品：\(product.productId) ，提交审核成功！提交ID：\(submission.id) ✅ ")
            if let subscription = await ascAPI.fetchSubscription(iapId: iapId),
               let state = subscription.attributes?.state {
                ascAPI.addMessage("订阅商品：\(product.productId) 提交后状态：\(state.rawValue)")
            }
        } else {
            ascAPI.addMessage("订阅商品：\(product.productId) ，提交审核失败！请检查商品是否为准备提交状态，或是否需要随 App 版本提交。❌ ")
            recordUploadFailure(product: product, reason: "订阅提交审核失败")
        }
    }

    /// 更新订阅商品的价格档位
    func updateSubscriptionPricePoint(iapId: String, product: IAPProduct, ascAPI: APASCAPI) async {
        guard let schedule = product.priceSchedules else {
            ascAPI.addMessage("无价格计划表：\(product.productId) ，请确认！❌ ")
            recordUploadFailure(product: product, reason: "价格计划表不存在")
            return
        }

        let baseTerritory = schedule.baseTerritory
        let baseCustomerPrice = schedule.baseCustomerPrice.normalizePrice()

        ascAPI.addMessage(
            "开始更新订阅商品价格点，基准国家：\(product.productId)，\(baseTerritory)，\(baseCustomerPrice) \n")

        let isPreservePrice = preserveCurrentPriceBtn.state.rawValue == 1
        ascAPI.addMessage("保留自动续期订阅者现有定价：\(isPreservePrice ? "是" : "否")")

        let points = await ascAPI.fetchSubscriptionPricePoints(
            iapId: iapId, territory: [baseTerritory])
        if let point = points.filter({
            $0.attributes?.customerPrice!.normalizePrice() == baseCustomerPrice
        }).first {

            ascAPI.addMessage("开始更新自定价格：")
            // 自定价格的国家或地区, 基准国家也算是自定价格
            var customerPriceSchedules = schedule.manualPrices
            customerPriceSchedules.append(
                IAPPricePoint(territory: baseTerritory, customerPrice: baseCustomerPrice))
            let manualPricesTerritory: [String] = customerPriceSchedules.map({ $0.territory })
            // 设置自定价格
            for pricePoint in customerPriceSchedules {
                let territory = pricePoint.territory
                let customerPrice = pricePoint.customerPrice.normalizePrice()
                let points = await ascAPI.fetchSubscriptionPricePoints(
                    iapId: iapId, territory: [territory])
                if let point = points.filter({
                    $0.attributes?.customerPrice!.normalizePrice() == customerPrice
                }).first {
                    if (await ascAPI.updateSubscriptionPricePoint(
                        iapId: iapId, pricePointId: point.id, preserveCurrentPrice: isPreservePrice))
                        != nil
                    {
                        ascAPI.addMessage("自定价格的订阅商品的价格点：\(territory)，\(customerPrice) ，更新价格成功！✅ ")
                    } else {
                        ascAPI.addMessage("自定价格的订阅商品的价格点：\(territory)，\(customerPrice) ，更新价格失败！❌ ")
                        recordUploadFailure(
                            product: product,
                            reason: "自定价格更新失败：\(territory) / \(customerPrice)")
                    }
                } else {
                    ascAPI.addMessage("自定价格的订阅商品价格点：\(territory)，\(customerPrice) ，未找到此档位！❌ ")
                    recordUploadFailure(
                        product: product,
                        reason: "自定价格未找到价格点：\(territory) / \(customerPrice)")
                }
            }

            ascAPI.addMessage("开始更新全球均衡价格：")
            // 剩余的所有的国家地区的订阅价格点，然后一个一个设置。API不支持全部国家一次配置
            let allPoints = await ascAPI.fetchSubscriptionPricePointsEqualizations(
                pointId: point.id, territory: nil)
            for apoint in allPoints {
                let territory = apoint.relationships?.territory?.data?.id ?? ""
                // 自定价格的国家跳过
                if manualPricesTerritory.contains(territory) {
                    continue
                }
                let customerPrice = apoint.attributes?.customerPrice ?? ""
                if (await ascAPI.updateSubscriptionPricePoint(
                    iapId: iapId, pricePointId: apoint.id, preserveCurrentPrice: isPreservePrice))
                    != nil
                {
                    // 价格档位配置成功
                    ascAPI.addMessage("全球均衡价格的订阅商品的价格点：\(territory)，\(customerPrice) ，更新价格成功！✅ ")
                } else {
                    // 价格档位配置失败
                    ascAPI.addMessage("全球均衡价格的订阅商品的价格点：\(territory)，\(customerPrice) ，更新价格失败！❌ ")
                    recordUploadFailure(
                        product: product,
                        reason: "全球均衡价格更新失败：\(territory) / \(customerPrice)")
                }
            }
        } else {
            // 找不到价格档位
            ascAPI.addMessage("基准国家的订阅商品价格点：\(baseTerritory)，\(baseCustomerPrice) ，未找到此档位！❌ ")
            recordUploadFailure(
                product: product,
                reason: "基准国家价格点未找到：\(baseTerritory) / \(baseCustomerPrice)")
        }
    }

    /// 将 Excel 中的一条基准介绍性优惠换算到商品的全部销售地区。
    /// 返回 false 时，上层不会自动提交审核，避免商品在缺少首周优惠时进入审核。
    func updateSubscriptionIntroductoryOffer(
        iapId: String, product: IAPProduct, ascAPI: APASCAPI
    ) async -> Bool {
        guard let offer = product.introductoryOffer else {
            return true
        }

        guard validateIntroductoryOffer(offer, product: product, ascAPI: ascAPI) else {
            return false
        }

        ascAPI.addMessage(
            "开始更新订阅商品首周优惠：\(product.productId)，基准国家：\(offer.baseTerritory)，首周价：\(offer.customerPrice.isEmpty ? "免费" : offer.customerPrice)，开始日期：\(offer.resolvedStartDate())，结束日期：\(offer.endDate.isEmpty ? "无期限" : offer.endDate)，优惠次数：\(offer.numberOfPeriods)")

        let targets: [IntroductoryOfferTarget]
        if offer.offerMode == "FREE_TRIAL" {
            let territoryIds: [String]
            if product.territories.availableInAllTerritories {
                territoryIds = await ascAPI.territories()?.map({ $0.id }) ?? []
            } else {
                territoryIds = product.territories.territories?.map({ $0.id }) ?? []
            }
            targets = territoryIds.map({
                IntroductoryOfferTarget(
                    territoryId: $0, pricePointId: nil, customerPrice: "免费")
            })
        } else {
            let price = offer.customerPrice.normalizePrice()
            let basePoints = await ascAPI.fetchSubscriptionPricePoints(
                iapId: iapId, territory: [offer.baseTerritory])
            guard let basePoint = basePoints.first(where: {
                $0.attributes?.customerPrice?.normalizePrice() == price
            }) else {
                ascAPI.addMessage(
                    "首周优惠未找到基准国家价格点：\(offer.baseTerritory)，\(price)！❌ ")
                recordUploadFailure(
                    product: product,
                    reason: "首周优惠未找到价格点：\(offer.baseTerritory) / \(price)")
                return false
            }

            var allPoints = await ascAPI.fetchSubscriptionPricePointsEqualizations(
                pointId: basePoint.id, territory: nil)
            allPoints.append(basePoint)

            let allowedTerritories: Set<String>? = product.territories.availableInAllTerritories
                ? nil
                : Set(product.territories.territories?.map({ $0.id }) ?? [])
            var seenTerritories = Set<String>()
            targets = allPoints.compactMap({ point in
                guard let territoryId = point.relationships?.territory?.data?.id,
                      territoryId.isNotEmpty,
                      allowedTerritories?.contains(territoryId) ?? true,
                      seenTerritories.insert(territoryId).inserted else {
                    return nil
                }
                return IntroductoryOfferTarget(
                    territoryId: territoryId,
                    pricePointId: point.id,
                    customerPrice: point.attributes?.customerPrice ?? "")
            })
        }

        guard targets.isNotEmpty else {
            ascAPI.addMessage("订阅商品：\(product.productId) 没有可配置首周优惠的销售地区！❌ ")
            recordUploadFailure(product: product, reason: "首周优惠没有可用销售地区")
            return false
        }

        guard var existingOffers = await ascAPI.fetchSubscriptionIntroductoryOffers(iapId: iapId)
        else {
            ascAPI.addMessage("订阅商品：\(product.productId) 无法查询已有首周优惠，已停止创建以避免重复！❌ ")
            recordUploadFailure(product: product, reason: "首周优惠已有配置查询失败")
            return false
        }
        var allSucceeded = true
        for target in targets {
            let territoryOffers = existingOffers.filter({
                $0.relationships?.territory?.data?.id == target.territoryId
            })
            if territoryOffers.contains(where: {
                introductoryOfferMatches(
                    existing: $0, requested: offer, target: target)
            }) {
                ascAPI.addMessage(
                    "首周优惠已经存在：\(target.territoryId)，\(target.customerPrice)，跳过重复创建 ✅ ")
                continue
            }

            if territoryOffers.contains(where: { introductoryOfferIsActive($0, requested: offer) }) {
                ascAPI.addMessage(
                    "首周优惠配置冲突：\(target.territoryId) 已存在另一套有效优惠，请先在 App Store Connect 中结束或删除旧优惠！❌ ")
                recordUploadFailure(
                    product: product,
                    reason: "首周优惠配置冲突：\(target.territoryId)")
                allSucceeded = false
                continue
            }

            if let created = await ascAPI.createSubscriptionIntroductoryOffer(
                iapId: iapId,
                territoryId: target.territoryId,
                pricePointId: target.pricePointId,
                offer: offer)
            {
                existingOffers.append(created)
                ascAPI.addMessage(
                    "首周优惠创建成功：\(target.territoryId)，\(target.customerPrice) ✅ ")
            } else {
                ascAPI.addMessage(
                    "首周优惠创建失败：\(target.territoryId)，\(target.customerPrice) ❌ ")
                recordUploadFailure(
                    product: product,
                    reason: "首周优惠创建失败：\(target.territoryId) / \(target.customerPrice)")
                allSucceeded = false
            }
        }
        return allSucceeded
    }

    private func validateIntroductoryOffer(
        _ offer: IAPIntroductoryOffer, product: IAPProduct, ascAPI: APASCAPI
    ) -> Bool {
        let validModes = Set(["PAY_AS_YOU_GO", "PAY_UP_FRONT", "FREE_TRIAL"])
        let validDurations = Set([
            "THREE_DAYS", "ONE_WEEK", "TWO_WEEKS", "ONE_MONTH",
            "TWO_MONTHS", "THREE_MONTHS", "SIX_MONTHS", "ONE_YEAR"
        ])
        var errors: [String] = []
        if offer.baseTerritory.isEmpty {
            errors.append("基准国家为空")
        }
        if validModes.contains(offer.offerMode) == false {
            errors.append("优惠模式无效：\(offer.offerMode)")
        }
        if validDurations.contains(offer.duration) == false {
            errors.append("优惠时长无效：\(offer.duration)")
        }
        let durationBySubscriptionPeriod = [
            "ONE_WEEK": "ONE_WEEK",
            "ONE_MONTH": "ONE_MONTH",
            "THREE_MONTHS": "THREE_MONTHS",
            "ONE_YEAR": "ONE_YEAR"
        ]
        if let subscriptionPeriod = product.subscriptions?.subscriptionPeriod,
           let expectedDuration = durationBySubscriptionPeriod[subscriptionPeriod],
           offer.duration != expectedDuration {
            errors.append(
                "优惠时长与订阅周期不一致：\(offer.duration)，应为 \(expectedDuration)")
        }
        if offer.numberOfPeriods <= 0 {
            errors.append("周期数必须大于 0")
        }
        if offer.offerMode != "FREE_TRIAL", offer.customerPrice.isEmpty {
            errors.append("付费优惠的首周价格为空")
        }
        if isValidOfferDate(offer.startDate) == false {
            errors.append("开始日期格式应为 YYYY-MM-DD：\(offer.startDate)")
        }
        if isValidOfferDate(offer.endDate) == false {
            errors.append("结束日期格式应为 YYYY-MM-DD：\(offer.endDate)")
        }
        if offer.startDate.isNotEmpty, offer.endDate.isNotEmpty,
           offer.startDate > offer.endDate {
            errors.append("开始日期不能晚于结束日期")
        }

        guard errors.isEmpty else {
            let reason = errors.joined(separator: "；")
            ascAPI.addMessage("订阅商品：\(product.productId) 首周优惠配置错误：\(reason)！❌ ")
            recordUploadFailure(product: product, reason: "首周优惠配置错误：\(reason)")
            return false
        }
        return true
    }

    private func isValidOfferDate(_ value: String) -> Bool {
        guard value.isNotEmpty else { return true }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        formatter.dateFormat = "yyyy-MM-dd"
        formatter.isLenient = false
        guard let date = formatter.date(from: value) else { return false }
        return formatter.string(from: date) == value
    }

    private func introductoryOfferMatches(
        existing: ASCSubscriptionIntroductoryOffer,
        requested: IAPIntroductoryOffer,
        target: IntroductoryOfferTarget
    ) -> Bool {
        guard existing.attributes?.offerMode?.rawValue == requested.offerMode,
              existing.attributes?.duration?.rawValue == requested.duration,
              existing.attributes?.numberOfPeriods == requested.numberOfPeriods else {
            return false
        }
        if requested.offerMode != "FREE_TRIAL",
           existing.relationships?.subscriptionPricePoint?.data?.id != target.pricePointId {
            return false
        }
        if requested.startDate.isNotEmpty,
           existing.attributes?.startDate != requested.startDate {
            return false
        }
        return (existing.attributes?.endDate ?? "") == requested.endDate
    }

    private func introductoryOfferIsActive(
        _ offer: ASCSubscriptionIntroductoryOffer,
        requested: IAPIntroductoryOffer
    ) -> Bool {
        guard let endDate = offer.attributes?.endDate else { return true }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        formatter.dateFormat = "yyyy-MM-dd"
        let today = formatter.string(from: Date())
        let requestedStart = requested.startDate.isEmpty ? today : requested.startDate
        return endDate >= requestedStart
    }

    /// 更新订阅商品的本地化信息
    func createSubscriptionLocalization(
        iapId: String, localization: IAPLocalization, product: IAPProduct, ascAPI: APASCAPI
    ) async {
        ascAPI.addMessage("开始更新订阅商品本地化版本：\(product.productId)，\(localization.locale)")
        if (await ascAPI.createSubscriptionLocalization(iapId: iapId, localization: localization))
            != nil
        {
            // 本地化语言配置成功
            ascAPI.addMessage("订阅商品本地化版本：\(localization.locale) ，更新语言成功！✅ ")
        } else {
            // 本地化语言配置失败
            ascAPI.addMessage("订阅商品本地化版本：\(localization.locale) ，更新语言失败！❌ ")
            recordUploadFailure(
                product: product,
                reason: "订阅本地化创建失败：\(localization.locale)")
        }
    }

    /// Apple 后台偶发会缓存订阅元数据状态，补一次无实际变更的更新请求来触发重新校验。
    func refreshSubscriptionMetadata(iapId: String, product: IAPProduct, ascAPI: APASCAPI) async {
        ascAPI.addMessage("开始刷新订阅商品元数据状态：\(product.productId)")

        guard (await ascAPI.updateSubscription(iapId: iapId, product: product)) != nil else {
            ascAPI.addMessage("订阅商品：\(product.productId) ，刷新基础元数据失败！❌ ")
            recordUploadFailure(product: product, reason: "订阅元数据刷新失败")
            return
        }

        guard let localization = product.subscriptionLocalization else {
            ascAPI.addMessage("订阅商品：\(product.productId) 无本地化元数据，跳过刷新本地化状态")
            return
        }

        let localizations = await ascAPI.fetchSubscriptionLocalizations(iapId: iapId)
        guard
            let locale = localizations.first(where: { $0.attributes?.locale == localization.locale }
            )
        else {
            ascAPI.addMessage("订阅商品：\(product.productId) ，未找到本地化 \(localization.locale)，跳过本地化刷新")
            return
        }

        if (await ascAPI.updateSubscriptionLocalization(
            iapLocaleId: locale.id, localization: localization)) != nil
        {
            ascAPI.addMessage("订阅商品：\(product.productId) ，元数据状态刷新成功！✅ ")
        } else {
            ascAPI.addMessage("订阅商品：\(product.productId) ，刷新本地化元数据失败！❌ ")
            recordUploadFailure(
                product: product,
                reason: "订阅本地化元数据刷新失败：\(localization.locale)")
        }
    }

    /// 更新订阅商品的送审截图
    func createSubscriptionScreenshot(iapId: String, product: IAPProduct, ascAPI: APASCAPI) async {
        ascAPI.addMessage("开始更新订阅商品的送审截图：\(product.productId)，\(product.reviewScreenshot)")
        let imgName = product.reviewScreenshot
        guard let imgPath = screenshotPaths[imgName] else {
            ascAPI.addMessage("订阅商品：\(product.productId) 无送审截图或未上传截图~")
            recordUploadFailure(product: product, reason: "未上传送审截图")
            return
        }

        let imaUrl = URL.init(fileURLWithPath: imgPath)
        let uploadFileName = imaUrl.lastPathComponent.isEmpty ? imgName : imaUrl.lastPathComponent
        guard let fileMD5 = URL.init(fileURLWithPath: imgPath).fileMD5() else {
            ascAPI.addMessage("订阅商品截图文件错误：\(imgPath) ，无法生成 md5 值~")
            recordUploadFailure(product: product, reason: "截图文件无法生成 MD5")
            return
        }

        let oldShot = await ascAPI.fetchSubscriptionScreenshot(iapId: iapId)
        // 存在需要删除，避免文件名不一样或者过期文件
        if let ost = oldShot {
            ascAPI.addMessage("删除旧的送审截图：\(ost.attributes?.fileName ?? "")")
            let status = await ascAPI.deleteSubscriptionScreenshot(iapShotId: ost.id)
            if status != 204 {
                ascAPI.addMessage("订阅商品截图创建失败：\(imgName) ，无法删除旧截图~")
                recordUploadFailure(product: product, reason: "送审截图删除旧图失败")
            }
        }

        ascAPI.addMessage("创建新的送审截图：\(uploadFileName)")
        // 创建截图
        let imaSize = imaUrl.fileSizeInt()
        guard imaSize > 0 else {
            ascAPI.addMessage("订阅商品截图文件错误：\(imgPath) ，文件大小为 0~")
            recordUploadFailure(product: product, reason: "截图文件大小为 0")
            return
        }
        guard
            let shot = await ascAPI.createSubscriptionScreenshot(
                iapId: iapId, fileName: uploadFileName, fileSize: imaSize)
        else {
            // 创建失败
            ascAPI.addMessage("订阅商品：\(product.productId) ，创建送审截图失败！❌ ")
            recordUploadFailure(product: product, reason: "送审截图创建失败")
            return
        }

        // 根据苹果接口返回的上传接口上传
        guard let method = shot.attributes?.uploadOperations?.first?.method,
            let url = shot.attributes?.uploadOperations?.first?.url,
            let requestHeaders = shot.attributes?.uploadOperations?.first?.requestHeaders,
            let baseURL = URL(string: url)
        else {
            ascAPI.addMessage("订阅商品：\(product.productId) ，创建送审截图失败！苹果参数异常~ ❌ ")
            recordUploadFailure(product: product, reason: "送审截图上传参数异常")
            return
        }

        var request = URLRequest(url: baseURL)
        request.httpMethod = method
        for header in requestHeaders {
            request.headers[header.name ?? ""] = header.value ?? ""
        }

        ascAPI.addMessage("上传新的送审截图：\(uploadFileName)")
        // 上传图片
        guard let response = try? await URLSession.shared.upload(for: request, fromFile: imaUrl)
        else {
            ascAPI.addMessage("订阅商品：\(product.productId) ，创建送审截图失败！上传图片异常~ ❌ ")
            recordUploadFailure(product: product, reason: "送审截图上传异常")
            return
        }
        guard let responseCode = (response.1 as? HTTPURLResponse)?.statusCode, responseCode == 200
        else {
            ascAPI.addMessage(
                "订阅商品：\(product.productId) ，创建送审截图失败！上传图片异常 \(response.1.description)~ ❌ ")
            recordUploadFailure(product: product, reason: "送审截图上传响应异常")
            return
        }

        ascAPI.addMessage("提交新的送审截图：\(uploadFileName)")
        // 确认图片
        if (await ascAPI.updateSubscriptionScreenshot(iapShotId: shot.id, fileMD5: fileMD5)) != nil
        {
            ascAPI.addMessage("订阅商品：\(product.productId) ，送审截图上传成功！✅ ")
        } else {
            ascAPI.addMessage("订阅商品：\(product.productId) ，送审截图可能上传失败！ ")
            recordUploadFailure(product: product, reason: "送审截图确认失败")
        }
    }

    /// 销售国家或地区
    func updateSubscriptionAvailableTerritories(
        iapId: String, product: IAPProduct, ascAPI: APASCAPI
    ) async {
        let inAll = product.territories.availableInAllTerritories
        let inNew = product.territories.availableInNewTerritories
        let summary = territoryInfo(product: product)
        let newTerritory = inNew ? "将来新国家(地区)时自动提供！" : "将来新国家(地区)时不自动提供!"
        ascAPI.addMessage("开始更新订阅商品的销售国家/地区：\(summary)")

        guard !inAll else {
            var allTerritories: [[String: String]] = []
            if let territories = await ascAPI.territories() {
                territories.forEach { territory in
                    allTerritories.append([
                        "type": "territories",
                        "id": territory.id,
                    ])
                }
                // 更新全部国家地区
                if (await ascAPI.updateSubscriptionAvailabilityTerritories(
                    iapId: iapId, availableTerritories: allTerritories,
                    availableInNewTerritories: inNew)) != nil
                {
                    ascAPI.addMessage("选择：所有国家(地区)销售，\(newTerritory)，更新成功！✅ ")
                } else {
                    ascAPI.addMessage("选择：所有国家(地区)销售，\(newTerritory)，更新失败！❌ ")
                    recordUploadFailure(product: product, reason: "销售国家/地区更新失败")
                }
            } else {
                ascAPI.addMessage("选择：所有国家(地区)销售，\(newTerritory)，无法设置！获取国家标识码失败！❌ ")
                recordUploadFailure(product: product, reason: "销售国家/地区列表获取失败")
            }
            return
        }

        /// 选择销售的国家或地区
        var territories: [[String: String]] = []
        product.territories.territories?.forEach({ territory in
            territories.append([
                "type": "territories",
                "id": territory.id,
            ])
        })

        let customerTerritory =
            product.territories.territories?.map({ $0.id }).joined(separator: ",") ?? "无"
        if (await ascAPI.updateSubscriptionAvailabilityTerritories(
            iapId: iapId, availableTerritories: territories, availableInNewTerritories: inNew))
            != nil
        {
            ascAPI.addMessage("订阅商品的销售国家/地区：\(customerTerritory) ，更新成功！✅ ")
        } else {
            ascAPI.addMessage("订阅商品的销售国家/地区：\(customerTerritory) ，更新失败！❌ ")
            recordUploadFailure(product: product, reason: "销售国家/地区更新失败")
        }
    }
}

// MARK: - Privacy Method
extension APUploadIAPListVC {

    func territoryInfo(product: IAPProduct) -> String {
        let inAll = product.territories.availableInAllTerritories
        let inNew = product.territories.availableInNewTerritories
        let customerTerritory =
            product.territories.territories?.map({ $0.id }).joined(separator: ",") ?? ""
        let off = !inAll && !inNew && (product.territories.territories?.isEmpty ?? true)
        let territory =
            off ? "下架" : (customerTerritory.isEmpty ? (inAll ? "全部" : "当前下架") : customerTerritory)
        let stringValue =
            "在所有国家/地区销售：'\(inAll ? "是" : "否")'\n将来新国家/地区自动提供：'\(inNew ? "是" : "否")'\n指定国家/地区销售：\(territory)"
        return stringValue
    }
}

// MARK: - NSTableViewDelegate
extension APUploadIAPListVC: NSTableViewDelegate, NSTableViewDataSource {
    func numberOfRows(in tableView: NSTableView) -> Int {
        return iaps.count
    }

    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int)
        -> NSView?
    {
        let iap = iaps[row]
        switch tableColumn?.identifier.enumValue() {
        case ColumnIdetifier.id.cellValue:
            let cell =
                tableView.makeView(withIdentifier: ColumnIdetifier.id.cellValue, owner: self)
                as? NSTableCellView
            cell?.textField?.stringValue = String(row + 1)
            return cell
        case ColumnIdetifier.productID.cellValue:
            let cell =
                tableView.makeView(withIdentifier: ColumnIdetifier.productID.cellValue, owner: self)
                as? NSTableCellView
            cell?.textField?.stringValue = iap.productId
            return cell
        case ColumnIdetifier.productName.cellValue:
            let cell =
                tableView.makeView(
                    withIdentifier: ColumnIdetifier.productName.cellValue, owner: self)
                as? NSTableCellView
            cell?.textField?.stringValue = iap.name
            return cell
        case ColumnIdetifier.price.cellValue:
            let cell =
                tableView.makeView(withIdentifier: ColumnIdetifier.price.cellValue, owner: self)
                as? NSTableCellView
            cell?.textField?.stringValue = territoryInfo(product: iap)
            return cell
        case ColumnIdetifier.level.cellValue:
            let cell =
                tableView.makeView(withIdentifier: ColumnIdetifier.level.cellValue, owner: self)
                as? NSTableCellView
            let territory = iap.priceSchedules?.baseTerritory ?? "-"
            let price = iap.priceSchedules?.baseCustomerPrice ?? "-"
            let customerPrice =
                iap.priceSchedules?.manualPrices.map({ pp in
                    "{'国家:'\(pp.territory)', '自定价格':'\(pp.customerPrice)'}\n"
                }).joined() ?? "-"
            cell?.textField?.stringValue = "基准国家：'\(territory)'\n基准价格：'\(price)'\n\(customerPrice)"
            return cell
        case ColumnIdetifier.productPds.cellValue:
            let cell =
                tableView.makeView(
                    withIdentifier: ColumnIdetifier.productPds.cellValue, owner: self)
                as? NSTableCellView
            cell?.textField?.stringValue = iap.reviewNote
            return cell
        case ColumnIdetifier.state.cellValue:
            let cell =
                tableView.makeView(withIdentifier: ColumnIdetifier.state.cellValue, owner: self)
                as? NSTableCellView
            cell?.textField?.stringValue = iap.inAppPurchaseType.CNValue()
            return cell
        case ColumnIdetifier.screenshot.cellValue:
            let cell =
                tableView.makeView(
                    withIdentifier: ColumnIdetifier.screenshot.cellValue, owner: self)
                as? ImageViewCell
            let file_name = iap.reviewScreenshot
            let imgPath = screenshotPaths[file_name] ?? ""
            cell?.imgSel.image = NSImage(contentsOfFile: imgPath)
            return cell
        case ColumnIdetifier.language.cellValue:
            let cell =
                tableView.makeView(withIdentifier: ColumnIdetifier.language.cellValue, owner: self)
                as? NSTableCellView
            cell?.textField?.stringValue = iap.localizations.map({ lz in
                "{'locale:'\(lz.locale)', 'title':'\(lz.name)', 'desc':'\(lz.description)'}\n"
            }).joined()
            return cell
        default:
            return nil
        }
    }

    func tableView(_ tableView: NSTableView, heightOfRow row: Int) -> CGFloat {
        let prices = iaps[row].priceSchedules?.manualPrices.count ?? 0
        let count = max(prices + 2, 3)
        return count > 10 ? CGFloat(20 * count) : CGFloat(25 * count)
    }
}
