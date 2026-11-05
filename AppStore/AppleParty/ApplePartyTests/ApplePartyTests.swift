//
//  ApplePartyTests.swift
//  ApplePartyTests
//
//  Created by HTC on 2022/3/10.
//  Copyright © 2022 37 Mobile Games. All rights reserved.
//

import XCTest
import CoreXLSX
@testable import AppleParty

class ApplePartyTests: XCTestCase {

    override func setUpWithError() throws {
        // Put setup code here. This method is called before the invocation of each test method in the class.
    }

    override func tearDownWithError() throws {
        // Put teardown code here. This method is called after the invocation of each test method in the class.
    }

    func testExample() throws {
        // This is an example of a functional test case.
        // Use XCTAssert and related functions to verify your tests produce the correct results.
        // Any test you write for XCTest can be annotated as throws and async.
        // Mark your test throws to produce an unexpected failure when your test encounters an uncaught error.
        // Mark your test async to allow awaiting for asynchronous code to complete. Check the results with assertions afterwards.
    }

    func testParseSubscriptionIntroductoryOffer() throws {
        let projectDirectory = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let demoURL = projectDirectory.appendingPathComponent("demo.xlsx")
        let file = try XCTUnwrap(XLSXFile(filepath: demoURL.path))

        let offers = IAPExcelParser.parserIntroductoryOffers(file)
        let offer = try XCTUnwrap(offers.first)

        XCTAssertEqual(offers.count, 2)
        XCTAssertEqual(offer.productId, "demo_bag_week_59.99_119.99_1")
        XCTAssertEqual(offer.baseTerritory, "USA")
        XCTAssertEqual(offer.customerPrice, "59.99")
        XCTAssertEqual(offer.offerMode, "PAY_AS_YOU_GO")
        XCTAssertEqual(offer.duration, "ONE_WEEK")
        XCTAssertEqual(offer.numberOfPeriods, 1)
        XCTAssertTrue(offer.startDate.isEmpty)
        XCTAssertTrue(offer.endDate.isEmpty)

        let utc = try XCTUnwrap(TimeZone(secondsFromGMT: 0))
        XCTAssertEqual(
            offer.resolvedStartDate(now: Date(timeIntervalSince1970: 0), timeZone: utc),
            "1970-01-01")
    }

    func testInferIntroductoryOfferFromProductID() throws {
        let schedule = IAPPriceSchedules(
            productId: "dinreel.vip_month_29.99_49.99_3",
            baseTerritory: "USA",
            baseCustomerPrice: "49.99")
        let offer = try XCTUnwrap(IAPExcelParser.inferredIntroductoryOffer(
            productId: schedule.productId,
            priceSchedule: schedule))

        XCTAssertEqual(offer.customerPrice, "29.99")
        XCTAssertEqual(offer.baseTerritory, "USA")
        XCTAssertEqual(offer.offerMode, "PAY_AS_YOU_GO")
        XCTAssertEqual(offer.duration, "ONE_MONTH")
        XCTAssertEqual(offer.numberOfPeriods, 1)
        XCTAssertTrue(offer.startDate.isEmpty)
        XCTAssertTrue(offer.endDate.isEmpty)
    }

    func testInferIntroductoryOfferFromAbnormalProductID() throws {
        let schedule = IAPPriceSchedules(
            productId: "dinreel.vip_quarter_abnormal_79.99_99.99_6",
            baseTerritory: "usa",
            baseCustomerPrice: "99.99")
        let offer = try XCTUnwrap(IAPExcelParser.inferredIntroductoryOffer(
            productId: schedule.productId,
            priceSchedule: schedule))

        XCTAssertEqual(offer.customerPrice, "79.99")
        XCTAssertEqual(offer.baseTerritory, "USA")
        XCTAssertEqual(offer.duration, "THREE_MONTHS")
    }

    func testInferIntroductoryOfferDurationFollowsVIPPeriod() throws {
        let cases = [
            ("dinreel.vip_week_9.99_29.99_3", "29.99", "ONE_WEEK"),
            ("dinreel.vip_month_29.99_49.99_3", "49.99", "ONE_MONTH"),
            ("dinreel.vip_quarter_79.99_99.99_3", "99.99", "THREE_MONTHS"),
            ("dinreel.vip_year_99.99_119.99_3", "119.99", "ONE_YEAR")
        ]

        for (productId, recurringPrice, expectedDuration) in cases {
            let schedule = IAPPriceSchedules(
                productId: productId,
                baseTerritory: "USA",
                baseCustomerPrice: recurringPrice)
            let offer = try XCTUnwrap(IAPExcelParser.inferredIntroductoryOffer(
                productId: productId,
                priceSchedule: schedule))
            XCTAssertEqual(offer.duration, expectedDuration, productId)
            XCTAssertEqual(offer.numberOfPeriods, 1, productId)
        }
    }

    func testDoesNotInferOfferWhenRecurringPriceDoesNotMatch() {
        let schedule = IAPPriceSchedules(
            productId: "dinreel.bag_week_59.99_119.99_1",
            baseTerritory: "USA",
            baseCustomerPrice: "99.99")

        XCTAssertNil(IAPExcelParser.inferredIntroductoryOffer(
            productId: schedule.productId,
            priceSchedule: schedule))
    }

    func testSharedDemoContainsThreeProductTypes() throws {
        let projectDirectory = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let demoURL = projectDirectory.appendingPathComponent("demo.xlsx")

        XCTAssertNotNil(XLSXFile(filepath: demoURL.path))
        let products = IAPExcelParser.parser(demoURL)

        XCTAssertEqual(products.count, 3)
        XCTAssertEqual(products.filter { $0.inAppPurchaseType == .AUTO_RENEWABLE }.count, 2)
        XCTAssertEqual(products.filter { $0.inAppPurchaseType == .CONSUMABLE }.count, 1)
    }

    func testPerformanceExample() throws {
        // This is an example of a performance test case.
        self.measure {
            // Put the code you want to measure the time of here.
        }
    }

}
