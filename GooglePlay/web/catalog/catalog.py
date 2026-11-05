#!/usr/bin/env python3
"""Excel-driven Google Play catalog workflow backed by gplay."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation


PRODUCT_HEADERS = [
    "enabled",
    "product_type",
    "product_id",
    "option_id",
    "billing_period",
    "grace_period",
    "base_region",
    "base_currency",
    "base_price",
    "activate",
    "age_rating",
    "content_type",
    "all_countries",
    "notes",
]
LOCALIZATION_HEADERS = [
    "enabled",
    "product_type",
    "product_id",
    "language_code",
    "title",
    "description",
    "benefits_pipe",
]
OFFER_HEADERS = [
    "enabled",
    "product_id",
    "base_plan_id",
    "offer_id",
    "phase_duration",
    "recurrence_count",
    "base_region",
    "base_currency",
    "base_price",
    "activate",
    "notes",
]
EXCEPTION_HEADERS = [
    "原始 Product ID",
    "Google Product ID",
    "周期/类型",
    "上传状态",
    "原因",
]
PRODUCT_TYPES = {"one_time", "subscription"}
PRODUCT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._]{0,62}$")
SUBSCRIPTION_PRODUCT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._]{0,39}$")
OPTION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
LANGUAGE_RE = re.compile(r"^[a-z]{2,3}(?:-[A-Z]{2})?$")
PERIOD_RE = re.compile(r"^P(?:0D|[1-9][0-9]*[DWMY])$")
AGE_RATING_TIERS = {
    "everyone": "PRODUCT_AGE_RATING_TIER_EVERYONE",
    "13+": "PRODUCT_AGE_RATING_TIER_THIRTEEN_AND_ABOVE",
    "16+": "PRODUCT_AGE_RATING_TIER_SIXTEEN_AND_ABOVE",
    "18+": "PRODUCT_AGE_RATING_TIER_EIGHTEEN_AND_ABOVE",
}
CONTENT_TYPES = {
    "digital_content": "WITHDRAWAL_RIGHT_DIGITAL_CONTENT",
    "service": "WITHDRAWAL_RIGHT_SERVICE",
}


class CatalogError(RuntimeError):
    pass


READ_ONLY_GPLAY_COMMANDS = {
    ("subscriptions", "list"),
    ("subscriptions", "get"),
    ("subscriptions", "batch-get"),
    ("onetimeproducts", "list"),
    ("onetimeproducts", "get"),
    ("onetimeproducts", "batch-get"),
    ("offers", "list"),
    ("offers", "get"),
    ("offers", "batch-get"),
    ("pricing", "convert"),
    ("pricing", "regions-version"),
}
TRANSIENT_GPLAY_MARKERS = (
    "error 429",
    "error 500",
    "error 502",
    "error 503",
    "error 504",
    "backenderror",
    "service is currently unavailable",
    "oauth2: cannot fetch token",
    "unexpected eof",
    "connection reset by peer",
    "context deadline exceeded",
    "i/o timeout",
)


def retryable_gplay_failure(args: list[str], detail: str) -> bool:
    return tuple(args[:2]) in READ_ONLY_GPLAY_COMMANDS and any(
        marker in detail.lower() for marker in TRANSIENT_GPLAY_MARKERS
    )


@dataclass(frozen=True)
class Product:
    product_type: str
    product_id: str
    option_id: str
    billing_period: str
    grace_period: str
    base_region: str
    base_currency: str
    base_price: Decimal
    activate: bool
    age_rating: str
    content_type: str
    all_countries: bool
    notes: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return self.product_type, self.product_id


@dataclass(frozen=True)
class Localization:
    product_type: str
    product_id: str
    language_code: str
    title: str
    description: str
    benefits: tuple[str, ...]

    @property
    def product_key(self) -> tuple[str, str]:
        return self.product_type, self.product_id


@dataclass(frozen=True)
class Offer:
    product_id: str
    base_plan_id: str
    offer_id: str
    phase_duration: str
    recurrence_count: int
    base_region: str
    base_currency: str
    base_price: Decimal
    activate: bool
    notes: str = ""

    @property
    def key(self) -> tuple[str, str, str]:
        return self.product_id, self.base_plan_id, self.offer_id


@dataclass
class Catalog:
    products: list[Product]
    localizations: dict[tuple[str, str], list[Localization]]
    offers: list[Offer]


class GPlay:
    def __init__(self, package: str, profile: str, cwd: Path):
        if shutil.which("gplay") is None:
            raise CatalogError("gplay is not installed or is not on PATH")
        self.package = package
        self.profile = profile
        self.cwd = cwd

    def run(self, args: list[str]) -> str:
        env = os.environ.copy()
        env["GPLAY_NO_UPDATE"] = "1"
        if self.profile:
            env["GPLAY_PROFILE"] = self.profile
        max_attempts = 5
        for attempt in range(max_attempts):
            result = subprocess.run(
                ["gplay", *args],
                cwd=self.cwd,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if result.returncode == 0:
                return result.stdout
            detail = result.stderr.strip() or result.stdout.strip()
            if (
                attempt == max_attempts - 1
                or not retryable_gplay_failure(args, detail)
            ):
                raise CatalogError(f"gplay {' '.join(args[:2])} failed: {detail}")
            time.sleep(min(2**attempt, 8))
        raise CatalogError(f"gplay {' '.join(args[:2])} failed unexpectedly")

    def json(self, args: list[str]) -> Any:
        output = self.run(args)
        try:
            value = json.loads(output or "{}")
        except json.JSONDecodeError as error:
            raise CatalogError(f"gplay returned invalid JSON: {error}") from error
        if value is None:
            return {}
        if not isinstance(value, (dict, list)):
            raise CatalogError("gplay returned an unsupported JSON response")
        return value

    def remote_catalog(self) -> tuple[dict[str, Any], dict[str, Any]]:
        subscription_response = self.json(
            ["subscriptions", "list", "--package", self.package, "--paginate"]
        )
        one_time_response = self.json(
            ["onetimeproducts", "list", "--package", self.package, "--paginate"]
        )
        subscriptions = (
            subscription_response
            if isinstance(subscription_response, list)
            else subscription_response.get("subscriptions", [])
        )
        one_time = (
            one_time_response
            if isinstance(one_time_response, list)
            else one_time_response.get("oneTimeProducts", [])
        )
        return (
            {item["productId"]: item for item in subscriptions},
            {item["productId"]: item for item in one_time},
        )

    def remote_offers(
        self,
        catalog: Catalog,
        subscriptions: dict[str, Any],
    ) -> dict[tuple[str, str, str], dict[str, Any]]:
        result: dict[tuple[str, str, str], dict[str, Any]] = {}
        parents = sorted(
            {
                (offer.product_id, offer.base_plan_id)
                for offer in catalog.offers
                if offer.product_id in subscriptions
                and any(
                    plan.get("basePlanId") == offer.base_plan_id
                    for plan in subscriptions[offer.product_id].get("basePlans", [])
                )
            }
        )
        for product_id, base_plan_id in parents:
            response = self.json(
                [
                    "offers",
                    "list",
                    "--package",
                    self.package,
                    "--product-id",
                    product_id,
                    "--base-plan-id",
                    base_plan_id,
                    "--paginate",
                ]
            )
            values = (
                response
                if isinstance(response, list)
                else response.get("subscriptionOffers", [])
            )
            for item in values:
                key = (product_id, base_plan_id, item.get("offerId", ""))
                result[key] = item
        return result

    def convert_prices(self, price: dict[str, Any]) -> dict[str, Any]:
        response = self.json(
            [
                "pricing",
                "convert",
                "--package",
                self.package,
                "--json",
                json.dumps({"price": price}, separators=(",", ":")),
            ]
        )
        if not isinstance(response, dict):
            raise CatalogError("pricing conversion returned an invalid response")
        version = response.get("regionVersion", {}).get("version", "")
        if not version or not response.get("convertedRegionPrices"):
            raise CatalogError("pricing conversion returned no regions or version")
        return response


def text_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def bool_value(value: Any, field: str, row_number: int, default: bool) -> bool:
    if value is None or text_value(value) == "":
        return default
    normalized = text_value(value).lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise CatalogError(f"row {row_number}: {field} must be yes/no or 1/0")


def decimal_value(value: Any, field: str, row_number: int) -> Decimal:
    try:
        result = Decimal(text_value(value))
    except (InvalidOperation, ValueError) as error:
        raise CatalogError(f"row {row_number}: {field} is not a valid price") from error
    if result <= 0:
        raise CatalogError(f"row {row_number}: {field} must be greater than zero")
    if abs(result.as_tuple().exponent) > 9:
        raise CatalogError(f"row {row_number}: {field} supports at most 9 decimals")
    return result


def positive_int_value(value: Any, field: str, row_number: int) -> int:
    raw = text_value(value)
    try:
        result = int(raw)
    except (TypeError, ValueError) as error:
        raise CatalogError(f"row {row_number}: {field} must be a positive integer") from error
    if str(result) != raw and raw not in {f"{result}.0"}:
        raise CatalogError(f"row {row_number}: {field} must be a positive integer")
    if result <= 0:
        raise CatalogError(f"row {row_number}: {field} must be a positive integer")
    return result


def worksheet_records(workbook: Any, sheet_name: str, headers: list[str]) -> Iterable[tuple[int, dict[str, Any]]]:
    if sheet_name not in workbook.sheetnames:
        raise CatalogError(f"missing worksheet: {sheet_name}")
    sheet = workbook[sheet_name]
    actual = [text_value(cell.value) for cell in sheet[1]]
    missing = [header for header in headers if header not in actual]
    if missing:
        raise CatalogError(f"{sheet_name}: missing columns: {', '.join(missing)}")
    indexes = {header: actual.index(header) for header in headers}
    for row_number, cells in enumerate(sheet.iter_rows(min_row=2), start=2):
        if all(cell.value is None for cell in cells):
            continue
        for cell in cells:
            if cell.data_type == "f":
                raise CatalogError(
                    f"{sheet_name} row {row_number}: formulas are not allowed"
                )
        yield row_number, {
            header: cells[indexes[header]].value
            for header in headers
            if indexes[header] < len(cells)
        }


def read_catalog(path: Path) -> Catalog:
    if not path.is_file():
        raise CatalogError(f"workbook not found: {path}")
    workbook = load_workbook(path, data_only=False, read_only=False)
    products: list[Product] = []
    product_keys: set[tuple[str, str]] = set()

    for row_number, row in worksheet_records(workbook, "products", PRODUCT_HEADERS):
        if not bool_value(row.get("enabled"), "enabled", row_number, True):
            continue
        product_type = text_value(row.get("product_type")).lower()
        product_id = text_value(row.get("product_id"))
        option_id = text_value(row.get("option_id"))
        billing_period = text_value(row.get("billing_period")).upper()
        grace_period = text_value(row.get("grace_period")).upper() or "P0D"
        base_region = text_value(row.get("base_region")).upper() or "US"
        base_currency = text_value(row.get("base_currency")).upper() or "USD"
        age_rating = text_value(row.get("age_rating")).lower()
        content_type = text_value(row.get("content_type")).lower()

        if product_type not in PRODUCT_TYPES:
            raise CatalogError(
                f"products row {row_number}: product_type must be one_time or subscription"
            )
        if not PRODUCT_ID_RE.fullmatch(product_id):
            raise CatalogError(f"products row {row_number}: invalid product_id")
        if (
            product_type == "subscription"
            and not SUBSCRIPTION_PRODUCT_ID_RE.fullmatch(product_id)
        ):
            raise CatalogError(
                f"products row {row_number}: subscription product_id must be at most 40 characters"
            )
        if not OPTION_ID_RE.fullmatch(option_id):
            raise CatalogError(f"products row {row_number}: invalid option_id")
        if product_type == "subscription" and not PERIOD_RE.fullmatch(billing_period):
            raise CatalogError(
                f"products row {row_number}: subscription billing_period must be ISO-8601, for example P1M"
            )
        if product_type == "one_time" and billing_period:
            raise CatalogError(
                f"products row {row_number}: one_time billing_period must be empty"
            )
        if not PERIOD_RE.fullmatch(grace_period):
            raise CatalogError(f"products row {row_number}: invalid grace_period")
        if not re.fullmatch(r"[A-Z]{2}", base_region):
            raise CatalogError(f"products row {row_number}: invalid base_region")
        if not re.fullmatch(r"[A-Z]{3}", base_currency):
            raise CatalogError(f"products row {row_number}: invalid base_currency")
        if age_rating and age_rating not in AGE_RATING_TIERS:
            raise CatalogError(
                f"products row {row_number}: age_rating must be everyone, 13+, 16+, or 18+"
            )
        if content_type and content_type not in CONTENT_TYPES:
            raise CatalogError(
                f"products row {row_number}: content_type must be digital_content or service"
            )
        all_countries = bool_value(
            row.get("all_countries"), "all_countries", row_number, True
        )
        if not all_countries:
            raise CatalogError(
                f"products row {row_number}: selective country availability is not supported; use yes"
            )
        key = (product_type, product_id)
        if key in product_keys:
            raise CatalogError(
                f"products row {row_number}: duplicate product {product_type}/{product_id}; this workflow supports one option/base plan per product"
            )
        product_keys.add(key)
        products.append(
            Product(
                product_type=product_type,
                product_id=product_id,
                option_id=option_id,
                billing_period=billing_period,
                grace_period=grace_period,
                base_region=base_region,
                base_currency=base_currency,
                base_price=decimal_value(
                    row.get("base_price"), "base_price", row_number
                ),
                activate=bool_value(
                    row.get("activate"), "activate", row_number, True
                ),
                age_rating=age_rating,
                content_type=content_type,
                all_countries=all_countries,
                notes=text_value(row.get("notes")),
            )
        )

    localizations: dict[tuple[str, str], list[Localization]] = {}
    localization_keys: set[tuple[str, str, str]] = set()
    for row_number, row in worksheet_records(
        workbook, "localizations", LOCALIZATION_HEADERS
    ):
        if not bool_value(row.get("enabled"), "enabled", row_number, True):
            continue
        product_type = text_value(row.get("product_type")).lower()
        product_id = text_value(row.get("product_id"))
        language_code = text_value(row.get("language_code"))
        title = text_value(row.get("title"))
        description = text_value(row.get("description"))
        benefits = tuple(
            item.strip()
            for item in text_value(row.get("benefits_pipe")).split("|")
            if item.strip()
        )
        product_key = (product_type, product_id)
        if product_key not in product_keys:
            raise CatalogError(
                f"localizations row {row_number}: product is missing or disabled"
            )
        if not LANGUAGE_RE.fullmatch(language_code):
            raise CatalogError(
                f"localizations row {row_number}: invalid language_code"
            )
        if not title:
            raise CatalogError(
                f"localizations row {row_number}: title is required"
            )
        localization_key = (product_type, product_id, language_code)
        if localization_key in localization_keys:
            raise CatalogError(
                f"localizations row {row_number}: duplicate localization"
            )
        localization_keys.add(localization_key)
        localizations.setdefault(product_key, []).append(
            Localization(
                product_type=product_type,
                product_id=product_id,
                language_code=language_code,
                title=title,
                description=description,
                benefits=benefits,
            )
        )

    offers: list[Offer] = []
    offer_keys: set[tuple[str, str, str]] = set()
    products_by_id = {product.product_id: product for product in products}
    if "offers" in workbook.sheetnames:
        for row_number, row in worksheet_records(workbook, "offers", OFFER_HEADERS):
            if not bool_value(row.get("enabled"), "enabled", row_number, True):
                continue
            product_id = text_value(row.get("product_id"))
            base_plan_id = text_value(row.get("base_plan_id"))
            offer_id = text_value(row.get("offer_id"))
            phase_duration = text_value(row.get("phase_duration")).upper()
            base_region = text_value(row.get("base_region")).upper() or "US"
            base_currency = text_value(row.get("base_currency")).upper() or "USD"
            product = products_by_id.get(product_id)
            if product is None or product.product_type != "subscription":
                raise CatalogError(
                    f"offers row {row_number}: product must be an enabled subscription"
                )
            if product.option_id != base_plan_id:
                raise CatalogError(
                    f"offers row {row_number}: base_plan_id must match products.option_id"
                )
            if not OPTION_ID_RE.fullmatch(offer_id):
                raise CatalogError(f"offers row {row_number}: invalid offer_id")
            if not PERIOD_RE.fullmatch(phase_duration) or phase_duration == "P0D":
                raise CatalogError(f"offers row {row_number}: invalid phase_duration")
            if not re.fullmatch(r"[A-Z]{2}", base_region):
                raise CatalogError(f"offers row {row_number}: invalid base_region")
            if not re.fullmatch(r"[A-Z]{3}", base_currency):
                raise CatalogError(f"offers row {row_number}: invalid base_currency")
            offer = Offer(
                product_id=product_id,
                base_plan_id=base_plan_id,
                offer_id=offer_id,
                phase_duration=phase_duration,
                recurrence_count=positive_int_value(
                    row.get("recurrence_count"), "recurrence_count", row_number
                ),
                base_region=base_region,
                base_currency=base_currency,
                base_price=decimal_value(
                    row.get("base_price"), "base_price", row_number
                ),
                activate=bool_value(
                    row.get("activate"), "activate", row_number, True
                ),
                notes=text_value(row.get("notes")),
            )
            if offer.key in offer_keys:
                raise CatalogError(f"offers row {row_number}: duplicate offer")
            offer_keys.add(offer.key)
            offers.append(offer)

    if not products:
        raise CatalogError("the workbook has no enabled products")
    for product in products:
        if product.key not in localizations:
            raise CatalogError(
                f"product {product.product_id} has no enabled localization"
            )
    return Catalog(products=products, localizations=localizations, offers=offers)


def workbook_package(path: Path) -> str:
    workbook = load_workbook(path, data_only=True, read_only=True)
    if "metadata" not in workbook.sheetnames:
        raise CatalogError("missing worksheet: metadata")
    values = {
        text_value(row[0]): text_value(row[1])
        for row in workbook["metadata"].iter_rows(values_only=True)
        if len(row) >= 2 and row[0] is not None
    }
    package = values.get("package", "")
    if not package:
        raise CatalogError("metadata: package is missing")
    return package


def require_workbook_package(path: Path, expected: str) -> None:
    actual = workbook_package(path)
    if actual != expected:
        raise CatalogError(
            f"workbook package is {actual}, but the target package is {expected}"
        )


def decimal_to_money(value: Decimal, currency: str) -> dict[str, Any]:
    units = int(value)
    nanos = int((value - Decimal(units)) * Decimal(1_000_000_000))
    return {"currencyCode": currency, "units": str(units), "nanos": nanos}


def money_to_decimal(value: dict[str, Any]) -> Decimal:
    return Decimal(str(value.get("units", "0"))) + (
        Decimal(str(value.get("nanos", 0))) / Decimal(1_000_000_000)
    )


def desired_listings(catalog: Catalog, product: Product) -> list[dict[str, Any]]:
    listings: list[dict[str, Any]] = []
    for item in sorted(
        catalog.localizations[product.key], key=lambda value: value.language_code
    ):
        listing: dict[str, Any] = {
            "languageCode": item.language_code,
            "title": item.title,
            "description": item.description,
        }
        if product.product_type == "subscription" and item.benefits:
            listing["benefits"] = list(item.benefits)
        listings.append(listing)
    return listings


def normalize_listings(items: Iterable[dict[str, Any]], subscription: bool) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in items or []:
        value = {
            "languageCode": item.get("languageCode", ""),
            "title": item.get("title", ""),
            "description": item.get("description", ""),
        }
        if subscription and item.get("benefits"):
            value["benefits"] = list(item["benefits"])
        normalized.append(value)
    return sorted(normalized, key=lambda value: value["languageCode"])


def remote_plan(remote: dict[str, Any], product: Product) -> dict[str, Any] | None:
    key = "basePlans" if product.product_type == "subscription" else "purchaseOptions"
    plans = remote.get(key, [])
    id_key = "basePlanId" if product.product_type == "subscription" else "purchaseOptionId"
    return next((item for item in plans if item.get(id_key) == product.option_id), None)


def remote_price(plan: dict[str, Any], product: Product) -> Decimal | None:
    key = (
        "regionalConfigs"
        if product.product_type == "subscription"
        else "regionalPricingAndAvailabilityConfigs"
    )
    regions = plan.get(key, [])
    value = next(
        (
            item.get("price")
            for item in regions
            if item.get("regionCode") == product.base_region
            and item.get("price", {}).get("currencyCode")
            == product.base_currency
        ),
        None,
    )
    return money_to_decimal(value) if value else None


def tax_compliance_for(product: Product) -> dict[str, Any]:
    value: dict[str, Any] = {}
    if product.age_rating:
        value["regionalProductAgeRatingInfos"] = [
            {
                "regionCode": "US",
                "productAgeRatingTier": AGE_RATING_TIERS[product.age_rating],
            }
        ]
    if product.product_type == "subscription" and product.content_type:
        value["eeaWithdrawalRightType"] = CONTENT_TYPES[product.content_type]
    return value


def compliance_matches(remote: dict[str, Any], product: Product) -> bool:
    actual = remote.get("taxAndComplianceSettings") or {}
    age = next(
        (
            item.get("productAgeRatingTier")
            for item in actual.get("regionalProductAgeRatingInfos", [])
            if item.get("regionCode") == "US"
        ),
        None,
    )
    if product.age_rating and age != AGE_RATING_TIERS[product.age_rating]:
        return False
    if product.product_type == "subscription" and product.content_type:
        return (
            actual.get("eeaWithdrawalRightType")
            == CONTENT_TYPES[product.content_type]
        )
    return True


def exported_age_rating(remote: dict[str, Any]) -> str:
    actual = remote.get("taxAndComplianceSettings") or {}
    tier = next(
        (
            item.get("productAgeRatingTier")
            for item in actual.get("regionalProductAgeRatingInfos", [])
            if item.get("regionCode") == "US"
        ),
        "",
    )
    return next(
        (label for label, value in AGE_RATING_TIERS.items() if value == tier), ""
    )


def exported_content_type(remote: dict[str, Any]) -> str:
    actual = remote.get("taxAndComplianceSettings") or {}
    value = actual.get("eeaWithdrawalRightType", "")
    return next(
        (label for label, enum_value in CONTENT_TYPES.items() if enum_value == value),
        "",
    )


def availability_matches(plan: dict[str, Any], product: Product) -> bool:
    if not product.all_countries:
        return True
    if product.product_type == "subscription":
        configs = plan.get("regionalConfigs", [])
        return (
            bool(configs)
            and all(item.get("newSubscriberAvailability") is True for item in configs)
            and plan.get("otherRegionsConfig", {}).get(
                "newSubscriberAvailability"
            )
            is True
        )
    configs = plan.get("regionalPricingAndAvailabilityConfigs", [])
    return (
        bool(configs)
        and all(item.get("availability") == "AVAILABLE" for item in configs)
        and plan.get("newRegionsConfig", {}).get("availability") == "AVAILABLE"
    )


def payload_for(catalog: Catalog, product: Product) -> dict[str, Any]:
    body: dict[str, Any] = {
        "listings": desired_listings(catalog, product),
        "taxAndComplianceSettings": tax_compliance_for(product),
    }
    if product.product_type == "one_time":
        body["purchaseOptions"] = [
            {
                "purchaseOptionId": product.option_id,
                "buyOption": {"legacyCompatible": True},
            }
        ]
    else:
        body["basePlans"] = [
            {
                "basePlanId": product.option_id,
                "autoRenewingBasePlanType": {
                    "billingPeriodDuration": product.billing_period,
                    "gracePeriodDuration": product.grace_period,
                    "legacyCompatible": True,
                    "resubscribeState": "RESUBSCRIBE_STATE_ACTIVE",
                    "prorationMode": "SUBSCRIPTION_PRORATION_MODE_CHARGE_ON_NEXT_BILLING_DATE",
                },
            }
        ]
    return body


def offer_payload_for(
    offer: Offer,
    converted: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if converted is None:
        phase_regions = [
            {
                "regionCode": offer.base_region,
                "price": decimal_to_money(offer.base_price, offer.base_currency),
            }
        ]
        offer_regions = [
            {"regionCode": offer.base_region, "newSubscriberAvailability": True}
        ]
        phase_other_regions = None
    else:
        phase_regions = []
        offer_regions = []
        for key, item in sorted(converted["convertedRegionPrices"].items()):
            region_code = item.get("regionCode") or key
            price = item.get("price")
            if not region_code or not price:
                continue
            phase_regions.append({"regionCode": region_code, "price": price})
            offer_regions.append(
                {"regionCode": region_code, "newSubscriberAvailability": True}
            )
        other_prices = converted.get("convertedOtherRegionsPrice")
        phase_other_regions = (
            {"otherRegionsPrices": other_prices} if other_prices else None
        )

    phase: dict[str, Any] = {
        "recurrenceCount": offer.recurrence_count,
        "duration": offer.phase_duration,
        "regionalConfigs": phase_regions,
    }
    if phase_other_regions:
        phase["otherRegionsConfig"] = phase_other_regions
    payload: dict[str, Any] = {
        "phases": [phase],
        "regionalConfigs": offer_regions,
        "targeting": {
            "acquisitionRule": {"scope": {"thisSubscription": {}}}
        },
    }
    if converted.get("convertedOtherRegionsPrice") if converted else False:
        payload["otherRegionsConfig"] = {
            "otherRegionsNewSubscriberAvailability": True
        }
    return payload


def expanded_subscription_fields(
    catalog: Catalog,
    product: Product,
    converted: dict[str, Any],
) -> dict[str, Any]:
    base_plan = payload_for(catalog, product)["basePlans"][0]
    configs: list[dict[str, Any]] = []
    for key, item in sorted(converted["convertedRegionPrices"].items()):
        region_code = item.get("regionCode") or key
        price = item.get("price")
        if region_code and price:
            configs.append(
                {
                    "regionCode": region_code,
                    "price": price,
                    "newSubscriberAvailability": True,
                }
            )
    base_plan["regionalConfigs"] = configs
    other = converted.get("convertedOtherRegionsPrice") or {}
    if other:
        base_plan["otherRegionsConfig"] = {
            "usdPrice": other.get("usdPrice"),
            "eurPrice": other.get("eurPrice"),
            "newSubscriberAvailability": True,
        }
    return {
        "basePlans": [base_plan],
        "taxAndComplianceSettings": tax_compliance_for(product),
    }


def remote_offer_price(remote: dict[str, Any], offer: Offer) -> Decimal | None:
    phases = remote.get("phases", [])
    if len(phases) != 1:
        return None
    value = next(
        (
            item.get("price")
            for item in phases[0].get("regionalConfigs", [])
            if item.get("regionCode") == offer.base_region
            and item.get("price", {}).get("currencyCode") == offer.base_currency
        ),
        None,
    )
    return money_to_decimal(value) if value else None


def offer_availability_matches(remote: dict[str, Any]) -> bool:
    phases = remote.get("phases", [])
    if len(phases) != 1:
        return False
    offer_regions = remote.get("regionalConfigs", [])
    phase_regions = phases[0].get("regionalConfigs", [])
    offer_codes = {item.get("regionCode") for item in offer_regions}
    phase_codes = {item.get("regionCode") for item in phase_regions}
    other_prices = (
        phases[0]
        .get("otherRegionsConfig", {})
        .get("otherRegionsPrices", {})
    )
    return (
        bool(offer_regions)
        and offer_codes == phase_codes
        and all(
            item.get("newSubscriberAvailability") is True
            for item in offer_regions
        )
        and remote.get("otherRegionsConfig", {}).get(
            "otherRegionsNewSubscriberAvailability"
        )
        is True
        and bool(other_prices.get("usdPrice"))
        and bool(other_prices.get("eurPrice"))
    )


def converted_price_point(
    gplay: GPlay | None,
    cache: dict[str, dict[str, Any]],
    base_price: Decimal,
    base_region: str,
    base_currency: str,
) -> Decimal | None:
    if gplay is None:
        return None
    converted = cached_converted_prices(
        gplay,
        cache,
        decimal_to_money(base_price, base_currency),
    )
    value = next(
        (
            item.get("price")
            for key, item in converted.get("convertedRegionPrices", {}).items()
            if (item.get("regionCode") or key) == base_region
            and item.get("price", {}).get("currencyCode") == base_currency
        ),
        None,
    )
    return money_to_decimal(value) if value else None


def build_offer_plan(
    catalog: Catalog,
    remote_offers: dict[tuple[str, str, str], dict[str, Any]],
    gplay: GPlay | None = None,
    converted_price_cache: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    price_cache = (
        converted_price_cache
        if converted_price_cache is not None
        else {}
    )
    items: list[dict[str, Any]] = []
    for offer in sorted(catalog.offers, key=lambda item: item.key):
        remote = remote_offers.get(offer.key)
        action = "create_offer"
        changes = ["offer does not exist"]
        activate_required = offer.activate
        if remote is not None:
            action = "noop"
            changes = []
            activate_required = offer.activate and remote.get("state") != "ACTIVE"
            phases = remote.get("phases", [])
            if len(phases) != 1:
                action = "blocked_offer_change"
                changes.append(f"remote offer has {len(phases)} phases; workbook requests 1")
            else:
                phase = phases[0]
                actual_duration = phase.get("duration", "")
                actual_recurrence = int(phase.get("recurrenceCount", 0))
                if actual_duration != offer.phase_duration:
                    action = "blocked_offer_change"
                    changes.append(
                        f"phase duration is {actual_duration}, workbook requests {offer.phase_duration}"
                    )
                if actual_recurrence != offer.recurrence_count:
                    action = "blocked_offer_change"
                    changes.append(
                        f"recurrence count is {actual_recurrence}, workbook requests {offer.recurrence_count}"
                    )
            actual_price = remote_offer_price(remote, offer)
            if actual_price != offer.base_price:
                normalized_price = converted_price_point(
                    gplay,
                    price_cache,
                    offer.base_price,
                    offer.base_region,
                    offer.base_currency,
                )
                if actual_price is not None and actual_price == normalized_price:
                    changes.append(
                        f"Google price point maps workbook price {offer.base_price} to {actual_price}"
                    )
                else:
                    action = "blocked_offer_change"
                    changes.append(
                        f"offer base price is {actual_price}, workbook requests {offer.base_price}"
                    )
            scope = remote.get("targeting", {}).get("acquisitionRule", {}).get(
                "scope", {}
            )
            if "thisSubscription" not in scope:
                action = "blocked_offer_change"
                changes.append("offer targeting is not new users of this subscription")
            if not offer_availability_matches(remote):
                action = "blocked_offer_change"
                changes.append("offer is not available in all current and future countries")
            if activate_required:
                changes.append("activation required")
        items.append(
            {
                "product_id": offer.product_id,
                "base_plan_id": offer.base_plan_id,
                "offer_id": offer.offer_id,
                "action": action,
                "activate_required": activate_required,
                "changes": changes,
                "payload": offer_payload_for(offer),
                "base_price": decimal_to_money(
                    offer.base_price, offer.base_currency
                ),
            }
        )
    return items


def build_plan(
    catalog: Catalog,
    workbook: Path,
    subscriptions: dict[str, Any],
    one_time: dict[str, Any],
    allow_one_time_price_changes: bool,
    remote_offers: dict[tuple[str, str, str], dict[str, Any]] | None = None,
    gplay: GPlay | None = None,
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    converted_price_cache: dict[str, dict[str, Any]] = {}
    desired_ids: dict[str, set[str]] = {"subscription": set(), "one_time": set()}
    for product in sorted(catalog.products, key=lambda item: item.key):
        remote_map = subscriptions if product.product_type == "subscription" else one_time
        remote = remote_map.get(product.product_id)
        desired_ids[product.product_type].add(product.product_id)
        action = "create"
        changes: list[str] = ["product does not exist"]
        activate_required = product.activate

        if remote is not None:
            changes = []
            plan = remote_plan(remote, product)
            if plan is None:
                if (
                    product.product_type == "subscription"
                    and not remote.get("basePlans", [])
                ):
                    action = "add_base_plan"
                    changes.append(f"create base plan {product.option_id}")
                else:
                    action = "blocked_structure_change"
                    changes.append(f"option/base plan {product.option_id} does not exist")
            else:
                action = "noop"
                activate_required = product.activate and plan.get("state") != "ACTIVE"
                if product.product_type == "subscription":
                    actual_auto = plan.get("autoRenewingBasePlanType", {})
                    actual_period = actual_auto.get("billingPeriodDuration", "")
                    if actual_period != product.billing_period:
                        action = "blocked_structure_change"
                        changes.append(
                            f"billing period is {actual_period}, workbook requests {product.billing_period}"
                        )
                    actual_grace = actual_auto.get("gracePeriodDuration", "P0D")
                    if actual_grace != product.grace_period:
                        action = "blocked_structure_change"
                        changes.append(
                            f"grace period is {actual_grace}, workbook requests {product.grace_period}"
                        )
                actual_price = remote_price(plan, product)
                if actual_price is None:
                    action = "blocked_price_change"
                    changes.append(
                        f"cannot find {product.base_region}/{product.base_currency} remote price"
                    )
                elif actual_price != product.base_price:
                    normalized_price = converted_price_point(
                        gplay,
                        converted_price_cache,
                        product.base_price,
                        product.base_region,
                        product.base_currency,
                    )
                    if actual_price == normalized_price:
                        changes.append(
                            f"Google price point maps workbook price {product.base_price} to {actual_price}"
                        )
                    else:
                        changes.append(
                            f"base price is {actual_price}, workbook requests {product.base_price}"
                        )
                        if product.product_type == "subscription":
                            action = "blocked_price_change"
                        elif allow_one_time_price_changes:
                            action = "update_price"
                        else:
                            action = "blocked_price_change"

                if not availability_matches(plan, product) or (
                    product.all_countries
                    and bool(remote.get("restrictedPaymentCountries"))
                ):
                    if not action.startswith("blocked_"):
                        action = "blocked_availability_change"
                    changes.append("not all current and future countries are available")

                wanted_listings = desired_listings(catalog, product)
                current_listings = normalize_listings(
                    remote.get("listings", []),
                    subscription=product.product_type == "subscription",
                )
                if wanted_listings != current_listings:
                    changes.append("localizations differ")
                    if action not in {
                        "blocked_structure_change",
                        "blocked_price_change",
                        "blocked_availability_change",
                        "update_price",
                    }:
                        action = "update_listings"
                if not compliance_matches(remote, product):
                    changes.append("age rating or content type differs")
                    if action not in {
                        "blocked_structure_change",
                        "blocked_price_change",
                        "blocked_availability_change",
                        "update_price",
                    }:
                        action = "update_metadata"
                elif not changes:
                    action = "noop"
                if activate_required:
                    changes.append("activation required")

        items.append(
            {
                "product_type": product.product_type,
                "product_id": product.product_id,
                "option_id": product.option_id,
                "action": action,
                "activate_required": activate_required,
                "changes": changes,
                "payload": payload_for(catalog, product),
                "base_price": decimal_to_money(
                    product.base_price, product.base_currency
                ),
            }
        )

    remote_only = {
        "subscriptions": sorted(set(subscriptions) - desired_ids["subscription"]),
        "one_time_products": sorted(set(one_time) - desired_ids["one_time"]),
    }
    offer_items = build_offer_plan(
        catalog,
        remote_offers or {},
        gplay,
        converted_price_cache,
    )
    counts: dict[str, int] = {}
    for item in [*items, *offer_items]:
        counts[item["action"]] = counts.get(item["action"], 0) + 1
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "workbook": str(workbook),
        "workbook_sha256": hashlib.sha256(workbook.read_bytes()).hexdigest(),
        "counts": counts,
        "remote_only_not_deleted": remote_only,
        "items": items,
        "offers": offer_items,
    }


def write_plan(plan: dict[str, Any], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    plan_path = output_dir / "plan.json"
    plan_path.write_text(
        json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    payload_dir = output_dir / "payloads"
    payload_dir.mkdir(exist_ok=True)
    for item in plan["items"]:
        name = f"{item['product_type']}__{item['product_id']}.json"
        (payload_dir / name).write_text(
            json.dumps(item["payload"], indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    for item in plan.get("offers", []):
        name = (
            f"offer__{item['product_id']}__{item['base_plan_id']}__"
            f"{item['offer_id']}.json"
        )
        (payload_dir / name).write_text(
            json.dumps(item["payload"], indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    return plan_path


def load_saved_plan(plan_path: Path, workbook: Path) -> dict[str, Any]:
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CatalogError(f"cannot read saved plan: {plan_path}") from error
    if (
        not isinstance(plan, dict)
        or plan.get("schema_version") != 1
        or not isinstance(plan.get("items"), list)
        or not isinstance(plan.get("offers"), list)
    ):
        raise CatalogError("saved plan has an unsupported format")
    workbook_sha256 = hashlib.sha256(workbook.read_bytes()).hexdigest()
    if plan.get("workbook_sha256") != workbook_sha256:
        raise CatalogError("saved plan does not match the current workbook")
    return plan


def completed_plan(plan: dict[str, Any], batch_size: int) -> dict[str, Any]:
    result = copy.deepcopy(plan)
    remaining_to_mark = batch_size
    for section in ("items", "offers"):
        for item in result.get(section, []):
            actionable = item.get("action") != "noop" or item.get(
                "activate_required", False
            )
            if not actionable or remaining_to_mark == 0:
                continue
            item["action"] = "noop"
            item["activate_required"] = False
            remaining_to_mark -= 1
    counts: dict[str, int] = {}
    for item in [*result.get("items", []), *result.get("offers", [])]:
        action = str(item.get("action", ""))
        counts[action] = counts.get(action, 0) + 1
    result["counts"] = counts
    return result


def workbook_style(workbook: Workbook) -> None:
    header_fill = PatternFill("solid", fgColor="1F4E78")
    for sheet in workbook.worksheets:
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for cell in sheet[1]:
            cell.fill = header_fill
            cell.font = Font(color="FFFFFF", bold=True)
            cell.alignment = Alignment(horizontal="center")
        for column in sheet.columns:
            width = min(
                max(len(text_value(cell.value)) for cell in column) + 2,
                56,
            )
            sheet.column_dimensions[column[0].column_letter].width = max(width, 12)


def write_workbook(
    output: Path,
    product_rows: list[list[Any]],
    localization_rows: list[list[Any]],
    package: str,
    offer_rows: list[list[Any]] | None = None,
    exception_rows: list[list[Any]] | None = None,
) -> None:
    workbook = Workbook()
    instructions = workbook.active
    instructions.title = "instructions"
    instructions.append(["Google Play catalog workflow"])
    instructions.append(
        ["1", "Edit only the products, localizations, offers, and 异常 sheets."]
    )
    instructions.append(["2", "Run validate, then plan. Plan is read-only."])
    instructions.append(["3", "Apply requires the exact package name as confirmation."])
    instructions.append(["Safety", "No product is deleted or automatically deactivated."])
    instructions.append(
        ["Safety", "Existing subscription price, period, or base-plan changes are blocked."]
    )
    instructions.append(
        ["Format", "product_type: one_time or subscription; billing periods use P1W/P1M/P1Y."]
    )

    products = workbook.create_sheet("products")
    products.append(PRODUCT_HEADERS)
    for row in product_rows:
        products.append(row)
    product_type_validation = DataValidation(
        type="list", formula1='"one_time,subscription"', allow_blank=False
    )
    boolean_validation = DataValidation(
        type="list", formula1='"yes,no"', allow_blank=False
    )
    age_validation = DataValidation(
        type="list", formula1='"everyone,13+,16+,18+"', allow_blank=True
    )
    content_validation = DataValidation(
        type="list", formula1='"digital_content,service"', allow_blank=True
    )
    products.add_data_validation(product_type_validation)
    products.add_data_validation(boolean_validation)
    products.add_data_validation(age_validation)
    products.add_data_validation(content_validation)
    product_type_validation.add("B2:B1000")
    boolean_validation.add("A2:A1000")
    boolean_validation.add("J2:J1000")
    boolean_validation.add("M2:M1000")
    age_validation.add("K2:K1000")
    content_validation.add("L2:L1000")

    localizations = workbook.create_sheet("localizations")
    localizations.append(LOCALIZATION_HEADERS)
    for row in localization_rows:
        localizations.append(row)
    localization_type_validation = DataValidation(
        type="list", formula1='"one_time,subscription"', allow_blank=False
    )
    localization_boolean_validation = DataValidation(
        type="list", formula1='"yes,no"', allow_blank=False
    )
    localizations.add_data_validation(localization_type_validation)
    localizations.add_data_validation(localization_boolean_validation)
    localization_type_validation.add("B2:B5000")
    localization_boolean_validation.add("A2:A5000")

    offers = workbook.create_sheet("offers")
    offers.append(OFFER_HEADERS)
    for row in offer_rows or []:
        offers.append(row)
    offer_boolean_validation = DataValidation(
        type="list", formula1='"yes,no"', allow_blank=False
    )
    offers.add_data_validation(offer_boolean_validation)
    offer_boolean_validation.add("A2:A1000")
    offer_boolean_validation.add("J2:J1000")

    exceptions = workbook.create_sheet("异常")
    exceptions.append(EXCEPTION_HEADERS)
    for row in exception_rows or []:
        exceptions.append(row)

    metadata = workbook.create_sheet("metadata")
    metadata.append(["key", "value"])
    metadata.append(["schema_version", "1"])
    metadata.append(["package", package])
    metadata.append(["generated_at", datetime.now(timezone.utc).isoformat()])
    metadata.sheet_state = "hidden"
    workbook_style(workbook)
    output.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(output)


def listing_rows(product_type: str, product_id: str, listings: list[dict[str, Any]]) -> list[list[Any]]:
    return [
        [
            "yes",
            product_type,
            product_id,
            item.get("languageCode", ""),
            item.get("title", ""),
            item.get("description", ""),
            "|".join(item.get("benefits", [])),
        ]
        for item in listings
    ]


def export_remote(gplay: GPlay, output: Path) -> None:
    subscriptions, one_time = gplay.remote_catalog()
    product_rows: list[list[Any]] = []
    localization_rows: list[list[Any]] = []
    offer_rows: list[list[Any]] = []

    for product_id, remote in sorted(one_time.items()):
        options = remote.get("purchaseOptions", [])
        if len(options) != 1:
            raise CatalogError(
                f"{product_id} has {len(options)} purchase options; exporter supports exactly one"
            )
        option = options[0]
        configs = option.get("regionalPricingAndAvailabilityConfigs", [])
        us = next((item for item in configs if item.get("regionCode") == "US"), None)
        if not us or not us.get("price"):
            raise CatalogError(f"{product_id} has no US base price")
        price = us["price"]
        product_rows.append(
            [
                "yes",
                "one_time",
                product_id,
                option.get("purchaseOptionId", ""),
                "",
                "P0D",
                "US",
                price.get("currencyCode", "USD"),
                str(money_to_decimal(price)),
                "yes" if option.get("state") == "ACTIVE" else "no",
                exported_age_rating(remote),
                "digital_content",
                "yes",
                "exported from Google Play",
            ]
        )
        localization_rows.extend(
            listing_rows("one_time", product_id, remote.get("listings", []))
        )

    for product_id, remote in sorted(subscriptions.items()):
        plans = remote.get("basePlans", [])
        if len(plans) != 1:
            raise CatalogError(
                f"{product_id} has {len(plans)} base plans; exporter supports exactly one"
            )
        plan = plans[0]
        configs = plan.get("regionalConfigs", [])
        us = next((item for item in configs if item.get("regionCode") == "US"), None)
        if not us or not us.get("price"):
            raise CatalogError(f"{product_id} has no US base price")
        price = us["price"]
        auto = plan.get("autoRenewingBasePlanType", {})
        product_rows.append(
            [
                "yes",
                "subscription",
                product_id,
                plan.get("basePlanId", ""),
                auto.get("billingPeriodDuration", ""),
                auto.get("gracePeriodDuration", "P0D"),
                "US",
                price.get("currencyCode", "USD"),
                str(money_to_decimal(price)),
                "yes" if plan.get("state") == "ACTIVE" else "no",
                exported_age_rating(remote),
                exported_content_type(remote),
                "yes",
                "exported from Google Play",
            ]
        )
        localization_rows.extend(
            listing_rows("subscription", product_id, remote.get("listings", []))
        )
        offer_response = gplay.json(
            [
                "offers",
                "list",
                "--package",
                gplay.package,
                "--product-id",
                product_id,
                "--base-plan-id",
                plan.get("basePlanId", ""),
                "--paginate",
            ]
        )
        offers = (
            offer_response
            if isinstance(offer_response, list)
            else offer_response.get("subscriptionOffers", [])
        )
        for offer in offers:
            phases = offer.get("phases", [])
            if len(phases) != 1:
                raise CatalogError(
                    f"{product_id}/{offer.get('offerId', '')} has {len(phases)} phases; exporter supports exactly one"
                )
            phase = phases[0]
            us_offer = next(
                (
                    item
                    for item in phase.get("regionalConfigs", [])
                    if item.get("regionCode") == "US" and item.get("price")
                ),
                None,
            )
            if not us_offer:
                raise CatalogError(
                    f"{product_id}/{offer.get('offerId', '')} has no paid US offer price"
                )
            offer_price = us_offer["price"]
            offer_rows.append(
                [
                    "yes",
                    product_id,
                    plan.get("basePlanId", ""),
                    offer.get("offerId", ""),
                    phase.get("duration", ""),
                    phase.get("recurrenceCount", 1),
                    "US",
                    offer_price.get("currencyCode", "USD"),
                    str(money_to_decimal(offer_price)),
                    "yes" if offer.get("state") == "ACTIVE" else "no",
                    "exported from Google Play",
                ]
            )
    write_workbook(
        output, product_rows, localization_rows, gplay.package, offer_rows
    )


APPLE_PERIODS = {
    "ONE_WEEK": "P1W",
    "ONE_MONTH": "P1M",
    "TWO_MONTHS": "P2M",
    "THREE_MONTHS": "P3M",
    "SIX_MONTHS": "P6M",
    "ONE_YEAR": "P1Y",
}
APPLE_REGIONS = {
    "USA": ("US", "USD"),
    "CHN": ("CN", "CNY"),
    "HKG": ("HK", "HKD"),
    "TWN": ("TW", "TWD"),
    "JPN": ("JP", "JPY"),
    "KOR": ("KR", "KRW"),
}


def sheet_dicts(workbook: Any, sheet_name: str) -> list[dict[str, Any]]:
    if sheet_name not in workbook.sheetnames:
        raise CatalogError(f"AppleParty workbook is missing worksheet: {sheet_name}")
    sheet = workbook[sheet_name]
    headers = [text_value(cell.value) for cell in sheet[1]]
    result: list[dict[str, Any]] = []
    for cells in sheet.iter_rows(min_row=2, values_only=True):
        if not any(value is not None for value in cells):
            continue
        result.append(
            {
                header: cells[index]
                for index, header in enumerate(headers)
                if header and index < len(cells)
            }
        )
    return result


def apple_region(value: Any, product_id: str) -> tuple[str, str]:
    code = text_value(value).upper()
    if code not in APPLE_REGIONS:
        raise CatalogError(
            f"{product_id}: unsupported Apple base region {code}; add an ISO/currency mapping first"
        )
    return APPLE_REGIONS[code]


def revised_product_id(product_id: str, revision: int | None) -> str:
    if revision is None:
        return product_id
    suffix = f"_{revision}"
    revised = re.sub(r"_[0-9]+$", suffix, product_id)
    if revised == product_id and not product_id.endswith(suffix):
        revised = f"{product_id}{suffix}"
    if not PRODUCT_ID_RE.fullmatch(revised):
        raise CatalogError(
            f"{product_id}: revision produces an invalid Google Play product ID: {revised}"
        )
    return revised


def compact_subscription_id(product_id: str) -> str:
    if SUBSCRIPTION_PRODUCT_ID_RE.fullmatch(product_id):
        return product_id
    compact = product_id.replace("_abnormal_", "_abn_")
    if not SUBSCRIPTION_PRODUCT_ID_RE.fullmatch(compact):
        compact = compact.replace("_quarter_", "_qtr_")
    if not SUBSCRIPTION_PRODUCT_ID_RE.fullmatch(compact):
        raise CatalogError(
            f"{product_id}: cannot generate a subscription product ID within Google's 40-character limit"
        )
    return compact


def exception_row(
    source_product_id: str,
    product_id: str,
    billing_period: str,
    product_type: str,
    status: str,
    reason: str,
) -> list[str]:
    period_name = {
        "P1W": "week",
        "P1M": "month",
        "P3M": "quarter",
        "P1Y": "year",
    }.get(billing_period, billing_period or "one_time")
    return [
        source_product_id,
        product_id,
        f"{period_name}/{product_type}",
        status,
        reason,
    ]


def update_exception_results(workbook_path: Path, plan: dict[str, Any]) -> None:
    workbook = load_workbook(workbook_path)
    if "异常" not in workbook.sheetnames:
        return

    product_items = {item["product_id"]: item for item in plan.get("items", [])}
    offers_by_product: dict[str, list[dict[str, Any]]] = {}
    for item in plan.get("offers", []):
        offers_by_product.setdefault(item["product_id"], []).append(item)

    sheet = workbook["异常"]
    changed = False
    for row_number in range(2, sheet.max_row + 1):
        source_product_id = text_value(sheet.cell(row_number, 1).value)
        google_product_id = text_value(sheet.cell(row_number, 2).value)
        if not google_product_id:
            continue

        mismatches: list[str] = []
        product_item = product_items.get(google_product_id)
        related_items = (
            [product_item] if product_item is not None else []
        ) + offers_by_product.get(google_product_id, [])
        if product_item is None:
            mismatches.append("商品不在本次 Google 工作簿中")
        for item in related_items:
            if item["action"] != "noop" or item.get("activate_required", False):
                details = item.get("changes") or [item["action"]]
                mismatches.extend(text_value(detail) for detail in details)

        if source_product_id != google_product_id:
            reason = "Google 订阅 ID 最长 40 个字符，已将 abnormal 缩写为 abn"
        else:
            reason = "源 Product ID 标记为 abnormal"
        if mismatches:
            status = "上传异常"
            reason += "；远端未匹配：" + "；".join(dict.fromkeys(mismatches))
        else:
            status = "已上传"
            reason += "；远端验证通过"

        if sheet.cell(row_number, 4).value != status:
            sheet.cell(row_number, 4).value = status
            changed = True
        if sheet.cell(row_number, 5).value != reason:
            sheet.cell(row_number, 5).value = reason
            changed = True

    if changed:
        workbook.save(workbook_path)


def import_appleparty(
    source: Path,
    output: Path,
    package: str,
    subscription_revision: int | None = None,
    one_time_revision: int | None = None,
    only_product_ids: set[str] | None = None,
) -> None:
    if not source.is_file():
        raise CatalogError(f"AppleParty workbook not found: {source}")
    workbook = load_workbook(source, data_only=True, read_only=True)
    product_source = sheet_dicts(workbook, "AppleParty")
    prices = {
        text_value(row.get("Product ID")): row
        for row in sheet_dicts(workbook, "PricePoints")
        if text_value(row.get("Product ID"))
    }
    introductory_rows = (
        sheet_dicts(workbook, "IntroductoryOffers")
        if "IntroductoryOffers" in workbook.sheetnames
        else []
    )
    introductory = {
        text_value(row.get("Product ID")): row
        for row in introductory_rows
        if text_value(row.get("Product ID"))
    }

    product_rows: list[list[Any]] = []
    localization_rows: list[list[Any]] = []
    offer_rows: list[list[Any]] = []
    exception_rows: list[list[Any]] = []
    imported_candidates: dict[
        tuple[str, str],
        tuple[tuple[Any, ...], tuple[Any, ...], tuple[Any, ...] | None],
    ] = {}
    for row in product_source:
        source_product_id = text_value(row.get("Product ID"))
        if not source_product_id:
            continue
        if only_product_ids is not None and source_product_id not in only_product_ids:
            if "_abnormal_" in source_product_id:
                skipped_is_subscription = (
                    text_value(row.get("应用内购买类型")) == "自动续期订阅"
                )
                skipped_period = (
                    APPLE_PERIODS.get(
                        text_value(row.get("订阅时限")).upper(), ""
                    )
                    if skipped_is_subscription
                    else ""
                )
                exception_rows.append(
                    exception_row(
                        source_product_id,
                        "",
                        skipped_period,
                        "subscription" if skipped_is_subscription else "one_time",
                        "未上传",
                        "未被选入本次导入",
                    )
                )
            continue
        apple_type = text_value(row.get("应用内购买类型"))
        is_subscription = apple_type == "自动续期订阅"
        product_type = "subscription" if is_subscription else "one_time"
        billing_period = (
            APPLE_PERIODS.get(text_value(row.get("订阅时限")).upper(), "")
            if is_subscription
            else ""
        )
        try:
            if not PRODUCT_ID_RE.fullmatch(source_product_id):
                raise CatalogError(
                    f"{source_product_id}: invalid Google Play product ID"
                )
            price_row = prices.get(source_product_id)
            if price_row is None:
                raise CatalogError(
                    f"{source_product_id}: missing PricePoints row"
                )
            region, currency = apple_region(
                price_row.get("基准国家(代码)"), source_product_id
            )
            base_price = decimal_value(
                price_row.get("基准国价格"), "base_price", 0
            )
            if not is_subscription and apple_type not in {"消耗型", "非消耗型"}:
                raise CatalogError(
                    f"{source_product_id}: unsupported purchase type {apple_type}"
                )
            product_id = revised_product_id(
                compact_subscription_id(source_product_id)
                if is_subscription
                else source_product_id,
                subscription_revision if is_subscription else one_time_revision,
            )
            grace_period = "P0D"
            option_id = "buy"
            if is_subscription:
                apple_period = text_value(row.get("订阅时限")).upper()
                if not billing_period:
                    raise CatalogError(
                        f"{product_id}: unsupported subscription period {apple_period}"
                    )
                option_id = product_id.replace(".", "").replace("_", "-")
                if not OPTION_ID_RE.fullmatch(option_id):
                    raise CatalogError(
                        f"{product_id}: generated base plan ID is invalid or too long"
                    )

            product_row = [
                "yes",
                product_type,
                product_id,
                option_id,
                billing_period,
                grace_period,
                region,
                currency,
                str(base_price),
                "yes",
                "16+",
                "digital_content",
                "yes",
                f"imported from AppleParty: {text_value(row.get('参考名字'))}",
            ]
            localization_row = [
                "yes",
                product_type,
                product_id,
                "en-US",
                text_value(row.get("订阅项国际化标题"))
                or text_value(row.get("参考名字")),
                text_value(row.get("订阅项国际化描述")),
                "",
            ]

            offer_row: list[Any] | None = None
            intro = introductory.get(source_product_id)
            if intro is not None:
                if not is_subscription:
                    raise CatalogError(
                        f"{product_id}: introductory offer requires a subscription"
                    )
                intro_region, intro_currency = apple_region(
                    intro.get("基准国家(代码)"), product_id
                )
                offer_period_name = text_value(intro.get("优惠时长")).upper()
                apple_phase_duration = APPLE_PERIODS.get(offer_period_name, "")
                if not apple_phase_duration:
                    raise CatalogError(
                        f"{product_id}: unsupported offer duration {offer_period_name}"
                    )
                offer_id = f"{option_id}-first"
                if not OPTION_ID_RE.fullmatch(offer_id):
                    raise CatalogError(
                        f"{product_id}: generated offer ID is invalid or too long"
                    )
                offer_row = [
                    "yes",
                    product_id,
                    option_id,
                    offer_id,
                    billing_period,
                    positive_int_value(
                        intro.get("周期数"), "recurrence_count", 0
                    ),
                    intro_region,
                    intro_currency,
                    str(decimal_value(intro.get("首周价格"), "offer_price", 0)),
                    "yes",
                    "imported from AppleParty IntroductoryOffers",
                ]

            key = (product_type, product_id)
            candidate = (
                tuple(product_row[:13]),
                tuple(localization_row),
                tuple(offer_row[:10]) if offer_row is not None else None,
            )
            previous = imported_candidates.get(key)
            if previous is not None:
                if previous == candidate:
                    continue
                exception_rows.append(
                    exception_row(
                        source_product_id,
                        "",
                        billing_period,
                        product_type,
                        "未上传",
                        "重复 Product ID 与已导入支付项内容不一致，已跳过",
                    )
                )
                continue

            imported_candidates[key] = candidate
            product_rows.append(product_row)
            localization_rows.append(localization_row)
            if offer_row is not None:
                offer_rows.append(offer_row)

            if "_abnormal_" in source_product_id:
                reason = "源 Product ID 标记为 abnormal"
                if product_id != source_product_id:
                    reason += "；Google 订阅 ID 最长 40 个字符，已缩写"
                exception_rows.append(
                    exception_row(
                        source_product_id,
                        product_id,
                        billing_period,
                        product_type,
                        "待上传",
                        reason,
                    )
                )
        except CatalogError as error:
            exception_rows.append(
                exception_row(
                    source_product_id,
                    "",
                    billing_period,
                    product_type,
                    "未上传",
                    str(error),
                )
            )
    write_workbook(
        output,
        product_rows,
        localization_rows,
        package,
        offer_rows,
        exception_rows,
    )

    if only_product_ids is not None:
        imported_source_ids = {
            text_value(row.get("Product ID"))
            for row in product_source
            if text_value(row.get("Product ID")) in only_product_ids
        }
        missing = sorted(only_product_ids - imported_source_ids)
        if missing:
            raise CatalogError(
                f"selected AppleParty product IDs were not found: {', '.join(missing)}"
            )


def template_workbook(output: Path, package: str) -> None:
    product_rows = [
        ["no", "one_time", "demo.coins_100", "coins-100", "", "P0D", "US", "USD", "0.99", "yes", "16+", "digital_content", "yes", "disabled example"],
        ["no", "subscription", "demo.premium_monthly", "monthly", "P1M", "P3D", "US", "USD", "4.99", "yes", "16+", "digital_content", "yes", "disabled example"],
    ]
    localization_rows = [
        ["no", "one_time", "demo.coins_100", "en-US", "100 Coins", "A pack of 100 coins", ""],
        ["no", "subscription", "demo.premium_monthly", "en-US", "Premium Monthly", "Monthly premium access", "No ads|Premium content"],
    ]
    offer_rows = [
        ["no", "demo.premium_monthly", "monthly", "intro-first-month", "P1M", 1, "US", "USD", "1.99", "yes", "disabled example"],
    ]
    write_workbook(output, product_rows, localization_rows, package, offer_rows)


def clear_payment_location_restrictions(
    gplay: GPlay,
    product: Product,
    regions_version: str,
) -> None:
    command = (
        ["subscriptions", "update"]
        if product.product_type == "subscription"
        else ["onetimeproducts", "patch"]
    )
    gplay.run(
        [
            *command,
            "--package",
            gplay.package,
            "--product-id",
            product.product_id,
            "--json",
            "{}",
            "--update-mask",
            "restrictedPaymentCountries",
            "--regions-version",
            regions_version,
        ]
    )


def cached_converted_prices(
    gplay: GPlay,
    cache: dict[str, dict[str, Any]],
    price: dict[str, Any],
) -> dict[str, Any]:
    key = json.dumps(price, sort_keys=True, separators=(",", ":"))
    if key not in cache:
        cache[key] = gplay.convert_prices(price)
    return cache[key]


def select_apply_batch(
    plan: dict[str, Any], batch_size: int
) -> tuple[dict[str, Any], int, int]:
    if batch_size < 1 or batch_size > 100:
        raise CatalogError("batch size must be between 1 and 100")

    work_items = [
        (section, item)
        for section in ("items", "offers")
        for item in plan.get(section, [])
        if item["action"] != "noop" or item.get("activate_required", False)
    ]
    selected_items = work_items[:batch_size]
    batch = dict(plan)
    batch["items"] = [item for section, item in selected_items if section == "items"]
    batch["offers"] = [
        item for section, item in selected_items if section == "offers"
    ]
    return batch, len(selected_items), len(work_items) - len(selected_items)


def apply_plan(
    gplay: GPlay,
    catalog: Catalog,
    plan: dict[str, Any],
    plan_dir: Path,
    batch_size: int,
) -> tuple[int, int]:
    blocked_products = [
        item
        for item in plan["items"]
        if item["action"].startswith("blocked_")
    ]
    blocked_offers = [
        item
        for item in plan.get("offers", [])
        if item["action"].startswith("blocked_")
    ]
    if blocked_products or blocked_offers:
        names = ", ".join(
            [item["product_id"] for item in blocked_products]
            + [
                f"{item['product_id']}/{item['offer_id']}"
                for item in blocked_offers
            ]
        )
        raise CatalogError(f"apply blocked by unsafe changes: {names}")
    batch, selected, remaining = select_apply_batch(plan, batch_size)
    products = {product.key: product for product in catalog.products}
    payload_dir = plan_dir / "payloads"
    converted_price_cache: dict[str, dict[str, Any]] = {}
    for item in batch["items"]:
        product = products[(item["product_type"], item["product_id"])]
        payload = payload_dir / f"{product.product_type}__{product.product_id}.json"
        action = item["action"]
        print(f"[{action}] {product.product_id}")
        if action in {"create", "update_price"}:
            command = (
                ["subscriptions", "create"]
                if product.product_type == "subscription"
                else ["onetimeproducts", "create"]
            )
            gplay.run(
                [
                    *command,
                    "--package",
                    gplay.package,
                    "--product-id",
                    product.product_id,
                    "--json",
                    f"@{payload}",
                    "--auto-convert-regional-prices",
                    "--base-price-json",
                    json.dumps(item["base_price"], separators=(",", ":")),
                ]
            )
            if product.all_countries:
                converted = cached_converted_prices(
                    gplay, converted_price_cache, item["base_price"]
                )
                clear_payment_location_restrictions(
                    gplay,
                    product,
                    converted["regionVersion"]["version"],
                )
        elif action == "add_base_plan":
            converted = cached_converted_prices(
                gplay, converted_price_cache, item["base_price"]
            )
            base_plan_payload = plan_dir / f"base-plan__{product.product_id}.json"
            base_plan_payload.write_text(
                json.dumps(
                    expanded_subscription_fields(catalog, product, converted),
                    indent=2,
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            gplay.run(
                [
                    "subscriptions",
                    "update",
                    "--package",
                    gplay.package,
                    "--product-id",
                    product.product_id,
                    "--json",
                    f"@{base_plan_payload}",
                    "--update-mask",
                    "basePlans,taxAndComplianceSettings,restrictedPaymentCountries",
                    "--regions-version",
                    converted["regionVersion"]["version"],
                ]
            )
        elif action in {"update_listings", "update_metadata"}:
            listing_payload = plan_dir / f"listings__{product.product_id}.json"
            listing_payload.write_text(
                json.dumps(
                    {
                        "listings": item["payload"]["listings"],
                        "taxAndComplianceSettings": item["payload"][
                            "taxAndComplianceSettings"
                        ],
                    },
                    indent=2,
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            command = (
                ["subscriptions", "update"]
                if product.product_type == "subscription"
                else ["onetimeproducts", "patch"]
            )
            command_args = [
                *command,
                "--package",
                gplay.package,
                "--product-id",
                product.product_id,
                "--json",
                f"@{listing_payload}",
                "--update-mask",
                "listings,taxAndComplianceSettings",
            ]
            if product.all_countries:
                converted = cached_converted_prices(
                    gplay, converted_price_cache, item["base_price"]
                )
                command_args[-1] += ",restrictedPaymentCountries"
                command_args.extend(
                    ["--regions-version", converted["regionVersion"]["version"]]
                )
            gplay.run(command_args)
        if item["activate_required"]:
            activation = plan_dir / f"activate__{product.product_id}.json"
            if product.product_type == "subscription":
                body = {
                    "requests": [
                        {
                            "activateBasePlanRequest": {
                                "packageName": gplay.package,
                                "productId": product.product_id,
                                "basePlanId": product.option_id,
                            }
                        }
                    ]
                }
                command = [
                    "baseplans",
                    "batch-update-states",
                    "--package",
                    gplay.package,
                    "--product-id",
                    product.product_id,
                ]
            else:
                body = {
                    "requests": [
                        {
                            "activatePurchaseOptionRequest": {
                                "packageName": gplay.package,
                                "productId": product.product_id,
                                "purchaseOptionId": product.option_id,
                            }
                        }
                    ]
                }
                command = [
                    "purchase-options",
                    "batch-update-states",
                    "--package",
                    gplay.package,
                    "--product-id",
                    product.product_id,
                ]
            activation.write_text(
                json.dumps(body, indent=2) + "\n", encoding="utf-8"
            )
            gplay.run([*command, "--json", f"@{activation}"])

    offers = {offer.key: offer for offer in catalog.offers}
    for item in batch.get("offers", []):
        key = (item["product_id"], item["base_plan_id"], item["offer_id"])
        offer = offers[key]
        action = item["action"]
        print(f"[{action}] {offer.product_id}/{offer.offer_id}")
        if action == "create_offer":
            converted = cached_converted_prices(
                gplay, converted_price_cache, item["base_price"]
            )
            payload = offer_payload_for(offer, converted)
            payload_path = (
                plan_dir
                / f"offer-expanded__{offer.product_id}__{offer.offer_id}.json"
            )
            payload_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            gplay.run(
                [
                    "offers",
                    "create",
                    "--package",
                    gplay.package,
                    "--product-id",
                    offer.product_id,
                    "--base-plan-id",
                    offer.base_plan_id,
                    "--offer-id",
                    offer.offer_id,
                    "--json",
                    f"@{payload_path}",
                    "--regions-version",
                    converted["regionVersion"]["version"],
                ]
            )
        if item["activate_required"]:
            gplay.run(
                [
                    "offers",
                    "activate",
                    "--package",
                    gplay.package,
                    "--product-id",
                    offer.product_id,
                    "--base-plan-id",
                    offer.base_plan_id,
                    "--offer-id",
                    offer.offer_id,
                ]
            )
    return selected, remaining


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Safe Excel workflow for Google Play products and subscriptions"
    )
    parser.add_argument(
        "--package",
        default="",
        help="Google Play applicationId",
    )
    parser.add_argument(
        "--profile",
        default="",
        help="gplay authentication profile",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    template = subparsers.add_parser("template", help="Create a disabled example workbook")
    template.add_argument("--output", type=Path, required=True)

    export = subparsers.add_parser("export", help="Export the current remote catalog to Excel")
    export.add_argument("--output", type=Path, required=True)

    appleparty = subparsers.add_parser(
        "import-appleparty", help="Convert an AppleParty workbook to Google Play format"
    )
    appleparty.add_argument("source", type=Path)
    appleparty.add_argument("--output", type=Path, required=True)
    appleparty.add_argument(
        "--subscription-revision",
        type=int,
        choices=range(1, 10000),
        metavar="N",
        help="replace or append the trailing revision number on subscription product IDs",
    )
    appleparty.add_argument(
        "--one-time-revision",
        type=int,
        choices=range(1, 10000),
        metavar="N",
        help="replace or append the trailing revision number on one-time product IDs",
    )
    appleparty.add_argument(
        "--only-product-id",
        action="append",
        dest="only_product_ids",
        help="import only this source Product ID; may be provided more than once",
    )

    validate = subparsers.add_parser("validate", help="Validate workbook structure and values")
    validate.add_argument("workbook", type=Path)

    plan = subparsers.add_parser("plan", help="Build a read-only remote diff and payloads")
    plan.add_argument("workbook", type=Path)
    plan.add_argument("--output-dir", type=Path, required=True)
    plan.add_argument("--allow-one-time-price-changes", action="store_true")

    apply = subparsers.add_parser("apply", help="Apply a fresh plan to Google Play")
    apply.add_argument("workbook", type=Path)
    apply.add_argument("--output-dir", type=Path, required=True)
    apply.add_argument("--confirm-package", required=True)
    apply.add_argument("--allow-one-time-price-changes", action="store_true")
    apply.add_argument(
        "--input-plan",
        type=Path,
        help="reuse a previously generated plan after validating the workbook hash",
    )
    apply.add_argument(
        "--batch-size",
        type=int,
        choices=range(1, 101),
        default=10,
        metavar="N",
        help="maximum changed catalog items to write in this run (default: 10)",
    )

    verify = subparsers.add_parser("verify", help="Verify workbook against Google Play")
    verify.add_argument("workbook", type=Path)
    verify.add_argument("--output-dir", type=Path, required=True)
    return parser


def plan_for_args(args: argparse.Namespace, catalog: Catalog, gplay: GPlay) -> dict[str, Any]:
    subscriptions, one_time = gplay.remote_catalog()
    remote_offers = gplay.remote_offers(catalog, subscriptions)
    return build_plan(
        catalog,
        args.workbook,
        subscriptions,
        one_time,
        getattr(args, "allow_one_time_price_changes", False),
        remote_offers,
        gplay,
    )


def main() -> int:
    args = create_parser().parse_args()
    root = Path.cwd()
    try:
        if not args.package:
            raise CatalogError("请选择目标 App，并通过 --package 指定 Android 包名")
        if args.command in {"export", "plan", "apply", "verify"} and not args.profile:
            raise CatalogError("请选择目标 App，并通过 --profile 指定 Google Play profile")
        if args.command == "template":
            template_workbook(args.output, args.package)
            print(json.dumps({"created": str(args.output)}, ensure_ascii=False))
            return 0

        if args.command == "import-appleparty":
            import_appleparty(
                args.source,
                args.output,
                args.package,
                args.subscription_revision,
                args.one_time_revision,
                set(args.only_product_ids) if args.only_product_ids else None,
            )
            catalog = read_catalog(args.output)
            print(
                json.dumps(
                    {
                        "created": str(args.output),
                        "products": len(catalog.products),
                        "offers": len(catalog.offers),
                    },
                    ensure_ascii=False,
                )
            )
            return 0

        if args.command == "export":
            gplay = GPlay(args.package, args.profile, root)
            export_remote(gplay, args.output)
            catalog = read_catalog(args.output)
            print(
                json.dumps(
                    {"created": str(args.output), "products": len(catalog.products)},
                    ensure_ascii=False,
                )
            )
            return 0

        catalog = read_catalog(args.workbook)
        require_workbook_package(args.workbook, args.package)
        if args.command == "validate":
            print(
                json.dumps(
                    {
                        "valid": True,
                        "products": len(catalog.products),
                        "localizations": sum(
                            len(items) for items in catalog.localizations.values()
                        ),
                        "offers": len(catalog.offers),
                    },
                    ensure_ascii=False,
                )
            )
            return 0

        gplay = GPlay(args.package, args.profile, root)
        plan = (
            load_saved_plan(args.input_plan, args.workbook)
            if args.command == "apply" and args.input_plan
            else plan_for_args(args, catalog, gplay)
        )
        plan_path = write_plan(plan, args.output_dir)
        print(json.dumps({"plan": str(plan_path), "counts": plan["counts"]}))

        if args.command == "plan":
            return 2 if any(key.startswith("blocked_") for key in plan["counts"]) else 0
        if args.command == "verify":
            update_exception_results(args.workbook, plan)
            product_mismatches = [
                item
                for item in plan["items"]
                if item["action"] != "noop" or item["activate_required"]
            ]
            offer_mismatches = [
                item
                for item in plan.get("offers", [])
                if item["action"] != "noop" or item["activate_required"]
            ]
            if product_mismatches or offer_mismatches:
                print(
                    "verify failed: "
                    + ", ".join(
                        [item["product_id"] for item in product_mismatches]
                        + [
                            f"{item['product_id']}/{item['offer_id']}"
                            for item in offer_mismatches
                        ]
                    ),
                    file=sys.stderr,
                )
                return 3
            print("verify passed: workbook matches Google Play")
            return 0
        if args.confirm_package != args.package:
            raise CatalogError(
                f"confirmation mismatch: pass --confirm-package {args.package}"
            )
        applied, remaining = apply_plan(
            gplay, catalog, plan, args.output_dir, args.batch_size
        )
        remaining_plan = completed_plan(plan, args.batch_size)
        remaining_plan_path = args.output_dir / "remaining-plan.json"
        remaining_plan_path.write_text(
            json.dumps(remaining_plan, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        if remaining:
            print(
                f"apply batch completed: {applied} item(s); {remaining} item(s) remain. "
                f"Continue with --input-plan {remaining_plan_path}."
            )
        else:
            print(
                f"apply completed: {applied} item(s) in the final batch; "
                "run verify as the final gate"
            )
        return 0
    except CatalogError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
