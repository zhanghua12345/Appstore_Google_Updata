//
//  IAPExcelParser.swift
//  AppleParty
//
//  Created by HTC on 2022/11/10.
//  Copyright © 2022 37 Mobile Games. All rights reserved.
//

import Foundation
import CoreXLSX


/// 上传的商品类型
enum IAPType: String {
    case CONSUMABLE = "CONSUMABLE"
    case NON_CONSUMABLE = "NON_CONSUMABLE"
    case NON_RENEWING_SUBSCRIPTION = "NON_RENEWING_SUBSCRIPTION"
    case AUTO_RENEWABLE = "auto-renewable"
    case UNKNOW = "unknown"
    
    static func type(name: String) -> IAPType {
        switch name {
        case "消耗型":
            return .CONSUMABLE
        case "非消耗型":
            return .NON_CONSUMABLE
        case "非续期订阅":
            return .NON_RENEWING_SUBSCRIPTION
        case "自动续期订阅":
            return .AUTO_RENEWABLE
        default:
            return .UNKNOW
        }
    }
    
    func CNValue() -> String {
        switch self {
        case .CONSUMABLE:
            return "消耗型"
        case .NON_CONSUMABLE:
            return "非消耗型"
        case .NON_RENEWING_SUBSCRIPTION:
            return "非续期订阅"
        case .AUTO_RENEWABLE:
            return "自动续期订阅"
        default:
            return "未知"
        }
    }
}

// 本地化名称和描述
struct IAPLocalization {
    var name: String = ""
    var description: String = ""
    var locale: String = ""
}

// 订阅类型的子字段
struct IAPSubscriptions {
    var groupLevel: Int = 1
    var subscriptionPeriod: String = "ONE_MONTH" //ONE_WEEK, ONE_MONTH, TWO_MONTHS, THREE_MONTHS, SIX_MONTHS, ONE_YEAR
}

/// 自动续期订阅的介绍性优惠。
/// 默认值适用于周订阅；月、季、年订阅的 duration 必须跟随订阅周期。
struct IAPIntroductoryOffer {
    var productId: String = ""
    var baseTerritory: String = ""
    var customerPrice: String = ""
    var offerMode: String = "PAY_AS_YOU_GO"
    var duration: String = "ONE_WEEK"
    var numberOfPeriods: Int = 1
    var startDate: String = ""
    var endDate: String = ""

    /// 开始日期留空时，以真正发起上传请求的当天为准，避免 Excel 写死日期后过期。
    func resolvedStartDate(now: Date = Date(), timeZone: TimeZone = .current) -> String {
        guard startDate.isEmpty else { return startDate }

        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = timeZone
        formatter.dateFormat = "yyyy-MM-dd"
        return formatter.string(from: now)
    }
}

/// 内购统一模型
struct IAPProduct {
    var name: String = ""
    var groupName: String = ""
    var productId: String = ""
    var reviewNote: String = ""
    var reviewScreenshot: String = ""
    var familySharable: Bool = false
    var inAppPurchaseType: IAPType = .UNKNOW  //# CONSUMABLE、NON_CONSUMABLE、NON_RENEWING_SUBSCRIPTION
    var groupLocalization: IAPLocalization?
    var subscriptionLocalization: IAPLocalization?
    
    var localizations: [IAPLocalization] = []
    // 订阅类型的特有
    var subscriptions: IAPSubscriptions?
    // 价格计划表
    var priceSchedules: IAPPriceSchedules?
    // 自动续期订阅的介绍性优惠
    var introductoryOffer: IAPIntroductoryOffer?
    // 销售的国家或地区
    var territories: IAPTerritories = IAPTerritories()
}

/// 价格计划表
struct IAPPriceSchedules {
    var productId: String = ""
    var baseTerritory: String = ""
    var baseCustomerPrice: String = ""
    var manualPrices: [IAPPricePoint] = []
}

/// 价格点
struct IAPPricePoint {
    var territory: String
    var customerPrice: String
}

/// 销售的国家或地区
struct IAPTerritories {
    var productId: String = ""
    /// 所有国家或地区销售(包括将来新国家或地区)
    var availableInAllTerritories: Bool = true
    /// 将来新国家/地区自动提供销售
    var availableInNewTerritories: Bool = true
    var territories: [IAPTerritory]?
}

struct IAPTerritory {
    var id: String
}


struct IAPExcelParser {
    
    static func parser(_ filePath: URL) -> [IAPProduct] {
        guard let file = XLSXFile(filepath: filePath.path) else {
          fatalError("XLSX file at \(filePath.path) is corrupted or does not exist")
        }
        
        // 先读取价格计划表
        let priceSchedules = parserPricePoints(file)
        // 自动续期订阅的介绍性优惠
        let introductoryOffers = parserIntroductoryOffers(file)
        // 销售的国家或地区
        let territories = parserTerritories(file)
        
        var result: [IAPProduct] = []
        for wbk in try! file.parseWorkbooks() {
            for (name, path) in try! file.parseWorksheetPathsAndNames(workbook: wbk) {
                if let worksheetName = name {
                    print("This worksheet has a name: \(worksheetName)")
                }
                
                if name != "AppleParty" {
                    continue
                }
                
                guard let worksheet = try? file.parseWorksheet(at: path) else {
                    print("This worksheet is null")
                    return result
                }
                let sharedStrings = try! file.parseSharedStrings()
                var index = 0
                var columnTitles = [String]()
                var columnIndexs = [String]()
                for row in worksheet.data?.rows ?? [] {
                    var columnValues = [String: String]()
                    index += 1
                    for cell in row.cells {
                        let key = cell.reference.column.value
                        let columnStrings = cellStringValue(cell, sharedStrings)
                        // 第一行作为标识行，用于多语言标识，默认信任此字段
                        if index == 1 {
                            columnTitles.append(columnStrings)
                            columnIndexs.append(key)
                        } else {
                            columnValues[key] = columnStrings
                        }
                    }
                    
                    if index == 1 {
                        print(columnTitles)
                        print(columnIndexs)
                        continue
                    }
                    
                    // Product ID     参考名字  内购买类型   审核截图(可选)   审核备注(可选)  zh-Hans    zh-Hans    ja    ja    ko    ko
                    var iap = IAPProduct()
                    iap.groupName = columnValues["A"] ?? ""
                    iap.productId = columnValues["B"] ?? ""
                    iap.name = columnValues["C"] ?? ""
                    let productType = columnValues["D"] ?? ""
                    iap.inAppPurchaseType = IAPType.type(name: productType)
                    
                    // 订阅类型默认的字段
                    if iap.inAppPurchaseType == .AUTO_RENEWABLE {
                        let period = columnValues["E"] ?? ""
                        iap.subscriptions = IAPSubscriptions()
                        iap.subscriptions?.subscriptionPeriod = period
                    }
                    
                    iap.reviewScreenshot = columnValues["F"] ?? ""
                    iap.reviewNote = columnValues["G"] ?? ""
                    
                    //订阅组国际化
                    if let text = columnValues["H"], !text.isEmpty {
                        let localization = IAPLocalization(name: text, description: "", locale: "en-US")
                        iap.groupLocalization = localization
                    }
                    // 商品本地化名称和描述。自动续期订阅和普通内购使用不同的上传接口。
                    if let text = columnValues["I"], !text.isEmpty {
                        let des = columnValues["J"] ?? ""
                        let localization = IAPLocalization(name: text, description: des, locale: "en-US")
                        if iap.inAppPurchaseType == .AUTO_RENEWABLE {
                            iap.subscriptionLocalization = localization
                        } else {
                            iap.localizations = [localization]
                        }
                    }
                    
                    // 非法的行
                    if iap.productId.isEmpty, iap.name.isEmpty {
                        continue
                    }
                    
                    // 【产品 ID】 可以由字母、数字、下划线（_）和句点（.）构成。 2 ~ 100 个字符）
                    if iap.productId.count < 2 || iap.productId.count > 100 {
                        NSAlert.show("Product ID 长度为：2~100 字符！")
                    }
                    // 参考名字
                    if iap.name.count < 2 || iap.name.count > 64 {
                        NSAlert.show("\(iap.productId)：“参考名字”长度超过 2~64 字符！")
                    }
                    
                    
                    
                    // 价格计划表
                    let schedules = priceSchedules.filter({ $0.productId == iap.productId })
                    if let schedule = schedules.first {
                        iap.priceSchedules = schedule
                    }

                    // 介绍性优惠
                    let offers = introductoryOffers.filter({ $0.productId == iap.productId })
                    if let offer = offers.first {
                        iap.introductoryOffer = offer
                    } else if iap.inAppPurchaseType == .AUTO_RENEWABLE {
                        iap.introductoryOffer = inferredIntroductoryOffer(
                            productId: iap.productId,
                            priceSchedule: iap.priceSchedules)
                    }
                    
                    //销售国家和地区
                    let territorys = territories.filter({ $0.productId == iap.productId })
                    if let territory = territorys.first {
                        iap.territories = territory
                    }
                    result.append(iap)
                }
            }
        }
        return result
    }

    /// 公共方法
    fileprivate static func handleRowContents(_ row: Row, _ sharedStrings: SharedStrings?, _ index: Int, _ columnIndexs: inout [String], _ columnValues: inout [String : String]) {
        for cell in row.cells {
            let key = cell.reference.column.value
            let columnStrings = cellStringValue(cell, sharedStrings)
            // 第一行作为标识行，用于多语言标识，默认信任此字段
            if index == 1 {
                columnIndexs.append(key)
            } else {
                columnValues[key] = columnStrings
            }
        }
    }

    /// CoreXLSX 旧版本需要分别处理共享字符串和 inlineStr。
    /// Excel、Numbers、WPS 以及 openpyxl 生成的文件可能使用不同的存储方式。
    fileprivate static func cellStringValue(_ cell: Cell, _ sharedStrings: SharedStrings?) -> String {
        guard let sharedStrings = sharedStrings else {
            return cell.inlineString?.text ?? cell.value ?? ""
        }

        var result = cell.stringValue(sharedStrings) ?? cell.inlineString?.text ?? cell.value ?? ""
        for richText in cell.richStringValue(sharedStrings) {
            result += string(from: richText.text)
        }
        return result
    }

    /// 兼容不增加工作表的旧模板：从 Product ID 中的“首周价_续订价_序号”推导首周优惠。
    /// 支持 bag/vip 的 week、month、quarter、year，以及带 abnormal 标记的会员商品。
    static func inferredIntroductoryOffer(
        productId: String,
        priceSchedule: IAPPriceSchedules?
    ) -> IAPIntroductoryOffer? {
        guard let priceSchedule = priceSchedule,
              priceSchedule.baseTerritory.isNotEmpty else {
            return nil
        }

        let components = productId.lowercased().split(separator: "_").map(String.init)
        guard let productFamily = components.first,
              productFamily.hasSuffix("bag") || productFamily.hasSuffix("vip"),
              let periodIndex = components.firstIndex(where: {
                  ["week", "month", "quarter", "year"].contains($0)
              }) else {
            return nil
        }

        var priceIndex = periodIndex + 1
        if components.indices.contains(priceIndex), components[priceIndex] == "abnormal" {
            priceIndex += 1
        }
        guard components.indices.contains(priceIndex + 2),
              Int(components.last ?? "") != nil else {
            return nil
        }

        let locale = Locale(identifier: "en_US_POSIX")
        let introductoryPrice = components[priceIndex].twoDecimalPrice()
        let recurringPrice = components[priceIndex + 1].twoDecimalPrice()
        guard Decimal(string: introductoryPrice, locale: locale) != nil,
              Decimal(string: recurringPrice, locale: locale) != nil,
              recurringPrice.normalizePrice()
                == priceSchedule.baseCustomerPrice.normalizePrice() else {
            return nil
        }

        let durationByPeriod = [
            "week": "ONE_WEEK",
            "month": "ONE_MONTH",
            "quarter": "THREE_MONTHS",
            "year": "ONE_YEAR"
        ]
        guard let duration = durationByPeriod[components[periodIndex]] else {
            return nil
        }

        return IAPIntroductoryOffer(
            productId: productId,
            baseTerritory: priceSchedule.baseTerritory.uppercased(),
            customerPrice: introductoryPrice,
            offerMode: "PAY_AS_YOU_GO",
            duration: duration,
            numberOfPeriods: 1,
            startDate: "",
            endDate: "")
    }
    
    /// 读取价格计划表
    static func parserPricePoints(_ file: XLSXFile) -> [IAPPriceSchedules]  {
        
        var result: [IAPPriceSchedules] = []
        for wbk in try! file.parseWorkbooks() {
            for (name, path) in try! file.parseWorksheetPathsAndNames(workbook: wbk) {
                if let worksheetName = name {
                    print("This worksheet has a name: \(worksheetName)")
                }
                
                if name != "PricePoints" {
                    continue
                }
                
                guard let worksheet = try? file.parseWorksheet(at: path) else {
                    print("This worksheet is null")
                    return result
                }
                let sharedStrings = try! file.parseSharedStrings()
                var index = 0
                var columnIndexs = [String]()
                for row in worksheet.data?.rows ?? [] {
                    var columnValues = [String: String]()
                    index += 1
                    handleRowContents(row, sharedStrings, index, &columnIndexs, &columnValues)
                    
                    // 第一行是标题行，忽视
                    if index == 1 {
                        print(columnIndexs)
                        continue
                    }
                    
                    // Product ID    基准国家(代码)    基准国价格    自定价格国家1    自定价格1    自定价格国家2    自定价格2
                    let productId = columnValues["A"] ?? ""
                    let baseTerritory = columnValues["B"] ?? ""
                    let baseCustomerPrice = (columnValues["C"] ?? "").twoDecimalPrice()
                                        
                    // 非法的行
                    if productId.isEmpty, baseTerritory.isEmpty, baseCustomerPrice.isEmpty {
                        continue
                    }
                    
                    // 【产品 ID】 可以由字母、数字、下划线（_）和句点（.）构成。 2 ~ 100 个字符）
                    if productId.count < 2 || productId.count > 100 {
                        NSAlert.show("PricePoints Product ID 长度为：2~100 字符！")
                    }
                    
                    // 价格计划表
                    var schedule = IAPPriceSchedules(productId: productId, baseTerritory: baseTerritory, baseCustomerPrice: baseCustomerPrice)
                    
                    // 自定价格的国家和价格
                    var manualPrices: [IAPPricePoint] = []
                    // 自定价格，从下标3开始，奇数遍历，成对出现的
                    let columeMax = columnValues.count
                    let columeEndIndex = columnValues.count - 1
                    for idx in stride(from: 3, to: columeEndIndex, by: 2) {
                        if idx + 1 <= columeMax, columnIndexs.count > idx+1 {
                            let key1 = columnIndexs[idx]
                            let key2 = columnIndexs[idx+1]
                            let name = columnValues[key1] ?? ""
                            let price = (columnValues[key2] ?? "").twoDecimalPrice()
                            if !name.isEmpty && !price.isEmpty {
                                let pricePoint = IAPPricePoint(territory: name, customerPrice: price)
                                manualPrices.append(pricePoint)
                            }
                        }
                    }
                    schedule.manualPrices = manualPrices
                    result.append(schedule)
                }
            }
        }
        return result
    }

    /// 读取自动续期订阅的介绍性优惠表。
    /// 列顺序：Product ID、基准国家、首周价格、优惠模式、优惠时长、周期数、开始日期、结束日期。
    static func parserIntroductoryOffers(_ file: XLSXFile) -> [IAPIntroductoryOffer] {
        var result: [IAPIntroductoryOffer] = []
        for wbk in try! file.parseWorkbooks() {
            for (name, path) in try! file.parseWorksheetPathsAndNames(workbook: wbk) {
                guard name == "IntroductoryOffers" else { continue }

                guard let worksheet = try? file.parseWorksheet(at: path) else {
                    print("This worksheet is null")
                    return result
                }
                let sharedStrings = try! file.parseSharedStrings()

                var index = 0
                var columnIndexs = [String]()
                for row in worksheet.data?.rows ?? [] {
                    var columnValues = [String: String]()
                    index += 1
                    handleRowContents(row, sharedStrings, index, &columnIndexs, &columnValues)

                    if index == 1 { continue }

                    let productId = columnValues["A"] ?? ""
                    let baseTerritory = (columnValues["B"] ?? "").uppercased()
                    let customerPrice = (columnValues["C"] ?? "").twoDecimalPrice()
                    let offerMode = (columnValues["D"] ?? "").uppercased()
                    let duration = (columnValues["E"] ?? "").uppercased()
                    let periodText = columnValues["F"] ?? ""
                    let startDate = columnValues["G"] ?? ""
                    let endDate = columnValues["H"] ?? ""

                    if productId.isEmpty, baseTerritory.isEmpty, customerPrice.isEmpty {
                        continue
                    }

                    let duplicate = result.contains(where: { $0.productId == productId })
                    if duplicate {
                        NSAlert.show("IntroductoryOffers 中 Product ID 重复：\(productId)。每个订阅只能配置一行基准优惠！")
                        continue
                    }

                    result.append(IAPIntroductoryOffer(
                        productId: productId,
                        baseTerritory: baseTerritory,
                        customerPrice: customerPrice,
                        offerMode: offerMode.isEmpty ? "PAY_AS_YOU_GO" : offerMode,
                        duration: duration.isEmpty ? "ONE_WEEK" : duration,
                        numberOfPeriods: Int(periodText) ?? 1,
                        startDate: startDate,
                        endDate: endDate
                    ))
                }
            }
        }
        return result
    }
    
    
    /// 读取销售的国家或地区
    static func parserTerritories(_ file: XLSXFile) -> [IAPTerritories]  {
        
        var result: [IAPTerritories] = []
        for wbk in try! file.parseWorkbooks() {
            for (name, path) in try! file.parseWorksheetPathsAndNames(workbook: wbk) {
                if let worksheetName = name {
                    print("This worksheet has a name: \(worksheetName)")
                }
                
                if name != "Territories" {
                    continue
                }
                
                guard let worksheet = try? file.parseWorksheet(at: path) else {
                    print("This worksheet is null")
                    return result
                }
                let sharedStrings = try! file.parseSharedStrings()
                var index = 0
                var columnIndexs = [String]()
                for row in worksheet.data?.rows ?? [] {
                    var columnValues = [String: String]()
                    index += 1
                    handleRowContents(row, sharedStrings, index, &columnIndexs, &columnValues)
                    
                    // 第一行是标题行，忽视
                    if index == 1 {
                        print(columnIndexs)
                        continue
                    }
                    
                    // Product ID    在所有国家/地区销售(1是，0否)    将来新国家/地区自动提供(1是，0否)    销售1    销售2  ...
                    let productId = columnValues["A"] ?? ""
                    let availableInAllTerritories = columnValues["B"] ?? ""
                    let availableInNewTerritories = columnValues["C"] ?? ""
                    let isInAll = availableInAllTerritories == "1" ? true : false
                    let isInNew = availableInNewTerritories == "1" ? true : false
                    
                    // 非法的行
                    if productId.isEmpty {
                        continue
                    }

                    // 【产品 ID】 可以由字母、数字、下划线（_）和句点（.）构成。 2 ~ 100 个字符）
                    if productId.count < 2 || productId.count > 100 {
                        NSAlert.show("Territories Product ID 长度为：2~100 字符！")
                    }
                    
                    // 销售的国家或地区
                    var territory = IAPTerritories(productId: productId, availableInAllTerritories: isInAll, availableInNewTerritories: isInNew)
                    
                    // 如果在所有国家或地区销售，则不在读取自定销售国家或地区
                    if isInAll {
                        territory.availableInNewTerritories = true
                        result.append(territory)
                        continue
                    }
                    
                    // 自定销售的国家或地区
                    var territories: [IAPTerritory] = []
                    // 从下标3开始
                    let columeMax = columnValues.count
                    for idx in stride(from: 3, to: columeMax, by: 1) {
                        let key = columnIndexs[idx]
                        let name = columnValues[key] ?? ""
                        if !name.isEmpty {
                            let pricePoint = IAPTerritory(id: name)
                            territories.append(pricePoint)
                        }
                    }
                    territory.territories = territories
                    result.append(territory)
                }
            }
        }
        return result
    }
}
