"use strict";

if (window.location.protocol === "file:") {
  document.querySelector("#file-warning").classList.remove("is-hidden");
} else {
  startApplication();
}

function startApplication() {
  const STORAGE_KEY = "google-play-excel-wizard-v3";
  const PREVIOUS_STORAGE_KEY = "google-play-excel-wizard-v2";
  const HELP_STORAGE_KEY = "google-play-excel-wizard-help-v1";
  const state = {
    config: null,
    apps: {},
    demo: null,
    appName: "",
    profile: "",
    selectedCredentialFile: null,
    credential: null,
    credentialReady: false,
    credentialChecked: false,
    job: null,
    currentStep: 0,
    pollTimer: null,
    lightweightStatusSupported: false,
  };

  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => Array.from(document.querySelectorAll(selector));

  const el = {
    appSelect: $("#app-select"),
    credentialFile: $("#credential-file"),
    credentialFileTitle: $("#credential-file-title"),
    credentialState: $("#credential-state"),
    credentialCaption: $("#credential-caption"),
    credentialEmpty: $("#credential-empty"),
    credentialTableWrap: $("#credential-table-wrap"),
    credentialBody: $("#credential-body"),
    checkCredentialButton: $("#check-credential-button"),
    packageInput: $("#package-input"),
    priceChangeInput: $("#price-change-input"),
    excelFile: $("#excel-file"),
    excelFileTitle: $("#excel-file-title"),
    excelState: $("#excel-state"),
    excelCaption: $("#excel-caption"),
    excelEmpty: $("#excel-empty"),
    excelTableWrap: $("#excel-table-wrap"),
    excelBody: $("#excel-body"),
    planTitle: $("#plan-title"),
    planSubtitle: $("#plan-subtitle"),
    checkExplanationText: $("#check-explanation span"),
    runPlanButton: $("#run-plan-button"),
    planMessage: $("#plan-message"),
    planEmpty: $("#plan-empty"),
    planTableWrap: $("#plan-table-wrap"),
    planBody: $("#plan-body"),
    submitStage: $("#submit-stage"),
    submitRemaining: $("#submit-remaining"),
    submitSuccessful: $("#submit-successful"),
    progressFill: $("#progress-fill"),
    confirmPackageInput: $("#confirm-package-input"),
    submitMessage: $("#submit-message"),
    submitBody: $("#submit-body"),
    downloadResult: $("#download-result"),
    helpDialog: $("#help-dialog"),
    backButton: $("#back-button"),
    nextButton: $("#next-button"),
    submitButton: $("#submit-button"),
    footerStatus: $("#footer-status"),
    recentEmpty: $("#recent-empty"),
    recentTableWrap: $("#recent-table-wrap"),
    recentBody: $("#recent-body"),
    toast: $("#toast"),
  };

  async function api(path, options = {}) {
    const request = { ...options, headers: { ...(options.headers || {}) } };
    if (request.method && request.method !== "GET") {
      request.headers["X-CSRF-Token"] = state.config.csrf_token;
    }
    const response = await fetch(path, request);
    const payload = (response.headers.get("content-type") || "").includes("application/json")
      ? await response.json()
      : null;
    if (!response.ok) {
      const error = new Error(payload?.error || `请求失败 (${response.status})`);
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  async function validateServiceAccountFile(file, expectedProjectId) {
    let credential;
    try {
      credential = JSON.parse(await file.text());
    } catch (_) {
      throw new Error("服务账号文件不是有效 JSON");
    }
    if (!credential || credential.type !== "service_account") {
      throw new Error("请选择 Google service account JSON 文件");
    }
    const required = ["project_id", "private_key_id", "private_key", "client_email", "token_uri"];
    for (const key of required) {
      if (typeof credential[key] !== "string" || !credential[key].trim()) {
        throw new Error("服务账号 JSON 缺少必要字段");
      }
    }
    if (!credential.private_key.includes("BEGIN PRIVATE KEY")) {
      throw new Error("服务账号 JSON 的 private_key 无效");
    }
    if (!credential.client_email.endsWith(".gserviceaccount.com")) {
      throw new Error("服务账号 JSON 的 client_email 无效");
    }
    if (credential.project_id !== expectedProjectId) {
      throw new Error(
        `服务账号 JSON 的 project_id 必须为 ${expectedProjectId}，当前文件为 ${credential.project_id}`
      );
    }
    return credential;
  }

  function showToast(message) {
    el.toast.textContent = message;
    el.toast.classList.remove("is-hidden");
    window.clearTimeout(showToast.timer);
    showToast.timer = window.setTimeout(() => el.toast.classList.add("is-hidden"), 4200);
  }

  function saveState() {
    if (!state.job?.id) {
      localStorage.removeItem(STORAGE_KEY);
      return;
    }
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        confirmationJobId: state.job.id,
        confirmPackage: el.confirmPackageInput.value.trim(),
      })
    );
  }

  function restoreState() {
    try {
      return JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}") || {};
    } catch (_) {
      return {};
    }
  }

  function formatDate(value) {
    if (!value) return "-";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
  }

  function setBadge(node, text, style = "") {
    node.textContent = text;
    node.className = `state-badge${style ? ` ${style}` : ""}`;
  }

  function statusPill(text, style = "") {
    const span = document.createElement("span");
    span.className = `status-pill${style ? ` ${style}` : ""}`;
    span.textContent = text;
    return span;
  }

  function appendCell(row, value, className = "") {
    const cell = document.createElement("td");
    cell.textContent = value === null || value === undefined || value === "" ? "-" : String(value);
    if (className) cell.className = className;
    row.appendChild(cell);
    return cell;
  }

  function appForProfile(profile) {
    return Object.keys(state.apps).find((name) => state.apps[name]?.name === profile) || "";
  }

  function maxUnlockedStep() {
    if (!state.credentialReady || !state.credential) return 0;
    if (!state.job?.validation) return 1;
    if (state.job?.operation === "plan") return 2;
    if (needsOnlineRecheck()) return 2;
    if (!state.job?.plan || state.job.plan.blocked > 0) return 2;
    return 3;
  }

  function isRunning() {
    return Boolean(state.job?.operation) || state.job?.status === "running";
  }

  function isSubmitOperation() {
    return ["apply", "apply_all", "verify"].includes(state.job?.operation);
  }

  function confirmDisruptiveAction(action) {
    if (!isSubmitOperation()) return true;
    const risk = action === "替换服务账号"
      ? "替换凭证还可能让后续批次鉴权失败。"
      : "后台上传仍会继续。";
    return window.confirm(
      `当前任务仍在后台自动上传（${state.job.stage}）。${action}会离开当前任务页面，${risk}\n\n确定继续吗？`
    );
  }

  function needsOnlineRecheck() {
    return ["uncertain", "error"].includes(state.job?.status) && !state.job?.operation;
  }

  function runningGuidance() {
    if (state.job?.operation === "prepare") return "步骤 2 正在校验 Excel，完成后会自动更新；请等待。";
    if (state.job?.operation === "plan") return "步骤 3 正在逐项读取商品、Base plan、首周优惠、激活状态和有效价格点，完成后会自动更新；请等待。";
    if (state.job?.operation === "verify") return "步骤 4 正在执行最终验证，无需返回或重复点击。";
    if (["apply", "apply_all"].includes(state.job?.operation)) {
      return `步骤 4 正在自动提交：${state.job.stage}。无需返回或重复点击。`;
    }
    return state.job?.stage || "任务正在服务端运行，请等待。";
  }

  function lockedStepGuidance() {
    if (!state.credentialReady || !state.credential) {
      return "无法进入步骤 2：请先在步骤 1 选择 App，并上传可用的服务账号 JSON。";
    }
    if (!state.job?.validation) {
      return "无法进入步骤 3：请先在步骤 2 上传并校验 Excel，等待本地校验通过。";
    }
    if (!state.job?.plan) {
      return "无法进入步骤 4：请先在步骤 3 完成 Google Play 只读差异检查。";
    }
    if (state.job.plan.blocked > 0) {
      return `无法进入步骤 4：步骤 3 有 ${state.job.plan.blocked} 个阻塞项。请返回步骤 2 修正 Excel、重新上传，再到步骤 3 检查。`;
    }
    return "当前步骤尚未完成，请按页面底部提示操作。";
  }

  function goToStep(step) {
    const allowed = maxUnlockedStep();
    if (step > allowed) {
      showToast(lockedStepGuidance());
      return;
    }
    state.currentStep = Math.max(0, Math.min(3, step));
    render();
    saveState();
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function renderNavigation() {
    const unlocked = maxUnlockedStep();
    $$(".step-nav__item").forEach((button) => {
      const step = Number(button.dataset.step);
      button.disabled = step > unlocked;
      button.classList.toggle("is-current", step === state.currentStep);
      button.classList.toggle("is-complete", step < unlocked || (step === 3 && state.job?.status === "verified"));
    });
    $$(".step-page").forEach((page) => {
      page.classList.toggle("is-hidden", Number(page.dataset.page) !== state.currentStep);
    });
  }

  function renderFooter() {
    const step = state.currentStep;
    const running = isRunning();
    el.backButton.disabled = step === 0 || running;
    el.nextButton.textContent = "下一步 →";
    el.nextButton.classList.toggle("is-hidden", step === 3);
    el.submitButton.classList.toggle("is-hidden", step !== 3);
    if (step === 0) {
      el.nextButton.disabled = !state.credentialReady;
      if (state.credentialReady) {
        el.footerStatus.textContent = "步骤 1 已完成，可以进入步骤 2 上传 Excel。";
      } else if (!state.appName && !state.selectedCredentialFile) {
        el.footerStatus.textContent = "请选择 App 和当前电脑上的服务账号 JSON。";
      } else if (!state.appName) {
        el.footerStatus.textContent = "本地 JSON 已选择，请再选择 App。";
      } else if (!state.selectedCredentialFile) {
        el.footerStatus.textContent = "App 已选择，请再选择当前电脑上的 JSON。";
      } else {
        el.footerStatus.textContent = "正在核对 App 与服务账号 JSON。";
      }
    } else if (step === 1) {
      const canContinue = Boolean(state.job?.validation) && state.job?.operation !== "prepare";
      el.nextButton.disabled = !canContinue;
      if (isSubmitOperation()) el.nextButton.textContent = "查看当前进度 →";
      el.footerStatus.textContent = running
        ? runningGuidance()
        : state.job?.validation
          ? "步骤 2 已完成，可以进入步骤 3 检查线上差异。"
          : "无法进入步骤 3：请先在步骤 2 上传并校验 Excel，等待本地校验通过。";
    } else if (step === 2) {
      const canViewSubmit = Boolean(state.job?.plan) && state.job.plan.blocked === 0 && state.job?.operation !== "plan" && !needsOnlineRecheck();
      el.nextButton.disabled = !canViewSubmit;
      if (isSubmitOperation()) el.nextButton.textContent = "查看提交进度 →";
      el.footerStatus.textContent = isSubmitOperation()
        ? runningGuidance()
        : state.job?.operation === "plan"
          ? runningGuidance()
          : needsOnlineRecheck()
            ? "无法继续提交：上次写入结果不确定。请返回步骤 3 点击“重新检查线上状态”，完成后再进入步骤 4。"
        : state.job?.plan?.blocked
          ? `无法进入步骤 4：有 ${state.job.plan.blocked} 个阻塞项。请返回步骤 2 修正 Excel 并重新上传。`
          : canViewSubmit
            ? "步骤 3 已完成，可以进入步骤 4 自动提交。"
            : "无法进入步骤 4：请先在步骤 3 执行只读差异检查。";
    } else {
      const plan = state.job?.plan;
      const verified = state.job?.status === "verified";
      const needsConfirmation = !verified;
      const confirmed = el.confirmPackageInput.value.trim() === state.job?.package;
      el.submitButton.textContent = verified
        ? "已完成"
        : isSubmitOperation()
          ? state.job?.operation === "verify" ? "最终验证进行中" : "自动提交进行中"
          : needsOnlineRecheck()
            ? "先重新检查线上状态"
        : plan?.actionable === 0
          ? "最终验证"
          : `自动提交全部 ${plan?.actionable || 0} 项`;
      el.submitButton.disabled = running || verified || needsOnlineRecheck() || !plan || plan.blocked > 0 || (needsConfirmation && !confirmed);
      el.footerStatus.textContent = verified
        ? "全部支付项已验证完成"
        : running
          ? runningGuidance()
          : needsOnlineRecheck()
            ? "无法继续提交：请返回步骤 3 点击“重新检查线上状态”，核对线上结果后再回到步骤 4。"
          : plan?.actionable
            ? "输入完全一致的包名，一次确认后自动完成全部同步"
            : "没有剩余差异，执行最终验证";
    }
  }

  function renderCredential() {
    const credential = state.credential;
    if (!credential) {
      const hasApp = Boolean(state.appName);
      const hasFile = Boolean(state.selectedCredentialFile);
      setBadge(el.credentialState, hasApp || hasFile ? "等待另一项" : "等待选择");
      el.credentialEmpty.classList.remove("is-hidden");
      el.credentialTableWrap.classList.add("is-hidden");
      el.checkCredentialButton.disabled = true;
      if (!hasApp && !hasFile) {
        el.credentialEmpty.textContent = "请选择 App 和当前电脑上的服务账号 JSON";
      } else if (!hasApp) {
        el.credentialEmpty.textContent = "本地 JSON 已选择，请再选择 App";
      } else if (!hasFile) {
        el.credentialEmpty.textContent = "App 已选择，请再选择当前电脑上的 JSON";
      } else {
        el.credentialEmpty.textContent = "正在核对 App 与服务账号 JSON";
      }
      el.credentialCaption.textContent = "两项都选择后才会读取并核对凭证。";
      el.credentialBody.replaceChildren();
      return;
    }
    const access = credential.api_access || (state.credentialChecked ? "通过" : "未检查");
    setBadge(
      el.credentialState,
      access === "通过" ? "Google 访问通过" : "凭证已读取",
      access === "通过" ? "is-success" : "is-running"
    );
    el.credentialEmpty.classList.add("is-hidden");
    el.credentialTableWrap.classList.remove("is-hidden");
    el.checkCredentialButton.disabled = false;
    const localFileName = state.selectedCredentialFile?.name || credential.file_name;
    el.credentialCaption.textContent = `${state.appName} · ${localFileName}`;
    el.credentialBody.replaceChildren();

    const systemRows = [
      ["App", state.appName, "已选择", false],
      ["Profile", state.profile, "已绑定", false],
      ["服务账号邮箱", credential.client_email, credential.file_status, false],
      ["Google Cloud 项目", credential.project_id, "已读取", false],
      ["本地凭证文件", localFileName, credential.file_status, false],
    ];
    systemRows.push([
      "Google Play 访问",
      credential.package || state.job?.package || el.packageInput.value,
      access,
      !["通过", "未检查"].includes(access),
    ]);
    systemRows.forEach(([name, value, status, warning]) => {
      const row = document.createElement("tr");
      appendCell(row, name);
      appendCell(row, value, warning ? "is-sensitive" : "");
      const cell = document.createElement("td");
      cell.appendChild(statusPill(status, status === "通过" || status === "已读取" || status === "已绑定" || status === "已选择" || status === "可用" ? "is-success" : warning ? "is-warning" : "is-blue"));
      row.appendChild(cell);
      el.credentialBody.appendChild(row);
    });
    if (credential.check_error) {
      const row = document.createElement("tr");
      appendCell(row, "访问失败原因");
      appendCell(row, credential.check_error, "is-sensitive");
      const cell = document.createElement("td");
      cell.appendChild(statusPill("需复查", "is-warning"));
      row.appendChild(cell);
      el.credentialBody.appendChild(row);
    }
  }

  function previewColumns(preview) {
    const desired = ["Product ID", "应用内购买类型", "订阅时限", "参考名字", "订阅项国际化标题", "订阅项国际化描述"];
    const available = desired.filter((name) => preview.columns.includes(name));
    return available.length >= 3 ? available : preview.columns.slice(0, 6);
  }

  function renderDemo() {
    if (!state.demo) return;
    const columns = previewColumns(state.demo);
    const headRow = document.createElement("tr");
    columns.forEach((column) => {
      const th = document.createElement("th");
      th.textContent = column;
      headRow.appendChild(th);
    });
    const statusHead = document.createElement("th");
    statusHead.textContent = "状态";
    headRow.appendChild(statusHead);
    $("#demo-head").replaceChildren(headRow);
    $("#demo-body").replaceChildren();
    state.demo.rows.forEach((item) => {
      const row = document.createElement("tr");
      columns.forEach((column) => appendCell(row, item[column]));
      const statusCell = document.createElement("td");
      statusCell.appendChild(statusPill("仅供格式参考", "is-blue"));
      row.appendChild(statusCell);
      $("#demo-body").appendChild(row);
    });
  }

  function priceText(value) {
    if (value === undefined || value === null || value === "") return "-";
    return String(value);
  }

  function catalogStatus(productId) {
    if (state.job?.error) return ["失败", "is-error"];
    if (state.job?.operation === "prepare") return ["校验中", "is-blue"];
    const planItems = state.job?.plan?.items?.filter((item) => item.product_id === productId) || [];
    if (planItems.some((item) => item.action?.startsWith("blocked_"))) return ["阻塞", "is-error"];
    if (planItems.some((item) => item.action !== "noop" || item.activate_required)) return ["待提交", "is-warning"];
    if (planItems.length && planItems.every((item) => item.action === "noop" && !item.activate_required)) return ["线上一致", "is-success"];
    if (state.job?.validation) return ["校验通过", "is-success"];
    return ["等待校验", ""];
  }

  function billingPeriodText(value) {
    return ({ P1W: "每周", P1M: "每月", P3M: "每 3 个月", P6M: "每 6 个月", P1Y: "每年" })[value] || value || "一次性";
  }

  function catalogPrice(item) {
    if (!item?.base_price) return "-";
    return `${item.base_currency || "USD"} ${priceText(item.base_price)}`;
  }

  function groupCatalogRows(products, offers) {
    const offersByProduct = new Map();
    offers.forEach((offer) => {
      if (!offersByProduct.has(offer.product_id)) offersByProduct.set(offer.product_id, []);
      offersByProduct.get(offer.product_id).push(offer);
    });
    return products.map((product) => ({
      product,
      offers: offersByProduct.get(product.product_id) || [],
    }));
  }

  function renderExcel() {
    const job = state.job;
    if (!job) {
      setBadge(el.excelState, "等待上传");
      el.excelEmpty.classList.remove("is-hidden");
      el.excelTableWrap.classList.add("is-hidden");
      el.excelBody.replaceChildren();
      el.excelCaption.textContent = "校验结果和每行状态会保留在这里。";
      return;
    }
    const running = job.operation === "prepare";
    setBadge(
      el.excelState,
      running ? "正在校验" : job.error && !job.validation ? "校验失败" : job.validation ? "校验通过" : "等待校验",
      running ? "is-running" : job.error && !job.validation ? "is-error" : job.validation ? "is-success" : ""
    );
    el.excelFileTitle.textContent = job.original_name;
    el.excelCaption.textContent = job.error || job.stage || "正在读取 Excel";
    const products = job.preview?.catalog?.rows || [];
    const previewOffers = job.preview?.offers?.rows || [];
    const offers = previewOffers.length ? previewOffers : (job.plan?.items || [])
      .filter((item) => item.kind === "offer")
      .map((item) => ({
        product_id: item.product_id,
        base_currency: item.currency_code,
        base_price: item.requested_price,
      }));
    const rows = groupCatalogRows(products, offers);
    el.excelEmpty.classList.toggle("is-hidden", rows.length > 0);
    el.excelTableWrap.classList.toggle("is-hidden", rows.length === 0);
    el.excelEmpty.textContent = running ? "正在解析工作簿…" : job.error || "等待生成预览";
    el.excelBody.replaceChildren();
    rows.forEach(({ product, offers: productOffers }) => {
      const row = document.createElement("tr");
      appendCell(row, product.product_id);
      appendCell(row, product.product_type === "one_time" ? "一次性商品" : `订阅 · ${billingPeriodText(product.billing_period)}`);
      appendCell(row, catalogPrice(product));
      appendCell(row, productOffers.length ? productOffers.map(catalogPrice).join("；") : "不适用");
      const [status, style] = catalogStatus(product.product_id);
      const statusCell = document.createElement("td");
      statusCell.appendChild(statusPill(status, style));
      row.appendChild(statusCell);
      el.excelBody.appendChild(row);
    });
  }

  const actionLabels = {
    noop: "无变化",
    create: "新建商品",
    update_price: "更新价格",
    add_base_plan: "新增方案",
    update_listings: "更新文案",
    update_metadata: "更新信息",
    create_offer: "新建优惠",
    update_offer: "更新优惠",
  };

  function translateChange(value) {
    if (value === "age rating or content type differs") return "年龄分级或内容类型不同";
    if (value === "offer does not exist") return "优惠不存在";
    const normalizedPrice = value.match(/^Google price point maps workbook price (.+) to (.+)$/);
    if (normalizedPrice) return `Google 已将 Excel 价格 ${normalizedPrice[1]} 自动调整为有效价格点 ${normalizedPrice[2]}`;
    const missingPlan = value.match(/^option\/base plan (.+) does not exist$/);
    if (missingPlan) return `购买选项/Base plan 不存在：${missingPlan[1]}`;
    return value;
  }

  function batchItemKey(item) {
    return [item.kind || "", item.product_id || "", item.base_plan_id || "", item.offer_id || ""].join("\u001f");
  }

  function groupSubmissionItems(items) {
    const groups = new Map();
    items.forEach((item) => {
      if (!groups.has(item.product_id)) {
        groups.set(item.product_id, {
          productId: item.product_id,
          originalPrice: null,
          firstWeekPrice: null,
          oneTime: false,
        });
      }
      const group = groups.get(item.product_id);
      if (item.kind === "offer") group.firstWeekPrice = item;
      else {
        group.originalPrice = item;
        group.oneTime = item.kind === "one_time";
      }
    });
    return Array.from(groups.values());
  }

  function fallbackPrices(productId) {
    const match = productId.match(/_(\d+(?:\.\d+)?)_(\d+(?:\.\d+)?)_[^_]+$/);
    return match ? { firstWeek: match[1], original: match[2] } : {};
  }

  function itemPriceDetail(item, label, fallbackPrice) {
    if (!item) return null;
    const normalized = (item.changes || [])
      .map((value) => String(value).match(/^Google price point maps workbook price (.+) to (.+)$/))
      .find(Boolean);
    const requested = item.requested_price || normalized?.[1] || fallbackPrice || "";
    const effective = item.effective_price || normalized?.[2] || requested;
    if (!requested) return `${label}：价格以 Excel 和 Google Play 最终结果为准`;
    const currency = item.currency_code || "USD";
    const adjustment = requested === effective ? "" : "（Google 价格点自动调整）";
    return `${label}：Excel ${currency} ${requested} → Google 有效价 ${currency} ${effective}${adjustment}`;
  }

  function submissionOutcome(item, label, activeKeys, refreshing, job) {
    if (!item) return null;
    if (job.status === "verified" || (item.action === "noop" && !item.activate_required)) {
      const successText = label === "原价"
        ? "原价上传成功"
        : label === "首周价" ? "首周价上传成功" : `${label}上传成功`;
      return { text: successText, state: "success" };
    }
    if (item.action.startsWith("blocked_")) {
      return { text: `${label}无法上传`, state: "failed" };
    }
    if (activeKeys.has(batchItemKey(item))) {
      return refreshing
        ? { text: `${label}结果不确定，正在自动核对`, state: "verifying" }
        : { text: `${label}正在写入 Google`, state: "writing" };
    }
    if (job.error && !job.operation) {
      return { text: `${label}上传未成功`, state: "failed" };
    }
    return { text: `${label}等待同步`, state: "pending" };
  }

  function groupedProgress(outcomes) {
    const states = outcomes.filter(Boolean).map((outcome) => outcome.state);
    if (states.includes("verifying")) return ["正在自动核对", "is-blue"];
    if (states.includes("writing")) return ["正在同步", "is-blue"];
    if (states.includes("failed")) return ["有未成功项", "is-error"];
    if (states.every((state) => state === "success")) return ["全部上传成功", "is-success"];
    if (states.includes("success")) return ["部分成功，继续处理中", "is-warning"];
    return ["等待同步", "is-warning"];
  }

  function planGroupStatus(group) {
    const items = [group.originalPrice, group.firstWeekPrice].filter(Boolean);
    if (items.some((item) => item.action.startsWith("blocked_"))) return ["阻塞", "is-error"];
    if (items.some((item) => item.action !== "noop" || item.activate_required)) return ["需要同步", "is-warning"];
    return ["线上一致", "is-success"];
  }

  function planGroupAction(group) {
    return [
      [group.oneTime ? "商品价格" : "原价", group.originalPrice],
      ["首周价", group.firstWeekPrice],
    ].filter(([, item]) => item).map(([label, item]) => {
      if (item.action.startsWith("blocked_")) return `${label}：禁止提交`;
      const action = actionLabels[item.action] || item.action;
      return `${label}：${action}${item.activate_required ? "并激活" : ""}`;
    }).join("；");
  }

  function planGroupChanges(group) {
    return [
      [group.oneTime ? "商品价格" : "原价", group.originalPrice],
      ["首周价", group.firstWeekPrice],
    ].filter(([, item]) => item).map(([label, item]) => {
      const changes = [...(item.changes || [])].map(translateChange);
      if (item.activate_required) changes.push("需要激活");
      return `${label}：${changes.join("；") || "与 Excel 一致"}`;
    }).join("；");
  }

  function renderPlan() {
    const plan = state.job?.plan;
    const checking = state.job?.operation === "plan";
    const jobRunning = isRunning();
    el.runPlanButton.disabled = jobRunning || !state.job?.validation;
    el.runPlanButton.textContent = checking
      ? "正在检查…"
      : isSubmitOperation()
        ? state.job?.operation === "verify" ? "最终验证进行中" : "自动提交进行中"
        : needsOnlineRecheck()
          ? "重新检查线上状态"
          : plan ? "重新检查差异" : "开始检查差异";
    const products = Number(state.job?.validation?.products || 0);
    const offers = Number(state.job?.validation?.offers || 0);
    el.checkExplanationText.textContent = `本任务需要从 Google Play 逐项读取 ${products || "全部"} 个商品与 Base plan 的存在、周期、文案、原价和激活状态，并检查 ${offers || "全部"} 个首周优惠及其激活状态，再核对 Excel 价格对应的 Google 有效价格点。读取次数多时需要几分钟；这里只读，不会修改线上数据。`;
    if (!plan) {
      const failed = Boolean(state.job?.error) && !checking;
      el.planTitle.textContent = checking ? "正在逐项读取 Google Play" : failed ? "线上检查失败" : "尚未检查差异";
      el.planSubtitle.textContent = checking
        ? `正在检查 ${products || "全部"} 个商品和 ${offers || "全部"} 个首周优惠；刷新页面不会中断。`
        : failed ? "原因和下一步处理方式见下方。" : "点击右侧按钮开始读取 Google Play。";
      ["#metric-total", "#metric-noop", "#metric-actionable", "#metric-blocked"].forEach((selector) => $(selector).textContent = "-");
      el.planEmpty.classList.remove("is-hidden");
      el.planTableWrap.classList.add("is-hidden");
      el.planBody.replaceChildren();
      el.planMessage.classList.toggle("is-hidden", !failed);
      if (failed) {
        el.planMessage.className = "message-band is-error";
        el.planMessage.textContent = `步骤 3 检查没有完成。原因：${state.job.error}。请留在步骤 3，确认网络和 Google Play 权限后点击“重新检查线上状态”；旧的步骤 4 数据已清空，不会被误提交。`;
      }
      return;
    }
    const noop = plan.items.filter((item) => item.action === "noop" && !item.activate_required).length;
    el.planTitle.textContent = isSubmitOperation()
      ? "步骤 4 正在自动提交"
      : needsOnlineRecheck()
        ? "需要重新检查线上状态"
        : plan.blocked ? "发现阻塞项" : plan.actionable ? "差异检查完成" : "线上已经一致";
    el.planSubtitle.textContent = isSubmitOperation()
      ? state.job.stage
      : `${plan.total} 项中，${plan.actionable} 项需要处理。`;
    $("#metric-total").textContent = plan.total;
    $("#metric-noop").textContent = noop;
    $("#metric-actionable").textContent = plan.actionable;
    $("#metric-blocked").textContent = plan.blocked;
    el.planMessage.className = `message-band ${plan.blocked || needsOnlineRecheck() ? "is-error" : "is-success"}`;
    el.planMessage.textContent = isSubmitOperation()
      ? "当前显示的是最近一次差异结果。步骤 4 正在服务端后台自动提交，请点击底部“查看提交进度”，无需重新检查或重复提交。"
      : needsOnlineRecheck()
        ? `上次写入结果不确定：${state.job.error || "Google Play 未返回确定结果"}。请点击“重新检查线上状态”，完成后进入步骤 4 继续；已经成功的项目会自动跳过。`
      : plan.blocked
      ? `有 ${plan.blocked} 个结构或安全阻塞项。请返回 Excel 修正并重新上传，阻塞清零前不能进入提交。`
      : plan.actionable
        ? "计划可以提交。确认一次后，服务端会直接同步全部内容并自动完成最终核对。"
        : "Excel 与 Google Play 完全一致，可以进入最后一步执行最终验证。";
    el.planMessage.classList.remove("is-hidden");
    el.planEmpty.classList.add("is-hidden");
    el.planTableWrap.classList.remove("is-hidden");
    el.planBody.replaceChildren();
    groupSubmissionItems(plan.items).forEach((group) => {
      const row = document.createElement("tr");
      appendCell(row, group.productId);
      appendCell(row, group.oneTime ? "商品价格" : group.firstWeekPrice ? "原价 + 首周价" : "原价");
      appendCell(row, planGroupAction(group));
      appendCell(row, planGroupChanges(group));
      const [status, style] = planGroupStatus(group);
      const statusCell = document.createElement("td");
      statusCell.appendChild(statusPill(status, style));
      row.appendChild(statusCell);
      el.planBody.appendChild(row);
    });
  }

  function renderSubmit() {
    const job = state.job;
    const plan = job?.plan;
    if (!job || !plan) return;
    const activeBatch = job.active_batch;
    const activeItems = activeBatch?.items || [];
    const activeKeys = new Set(activeItems.map(batchItemKey));
    const refreshing = activeBatch?.phase === "refreshing";
    const remaining = plan.actionable;
    const applied = job.applied_total || 0;
    const totalWork = Math.max(1, applied + remaining);
    const percent = job.status === "verified" ? 100 : Math.round((applied / totalWork) * 100);
    el.submitStage.textContent = activeBatch
      ? refreshing
        ? "Google 暂时未返回确定结果，正在自动核对后继续"
        : `正在向 Google Play 同步（${activeItems.length} 项）`
      : job.stage;
    el.submitRemaining.textContent = remaining;
    el.submitSuccessful.textContent = job.status === "verified" ? groupSubmissionItems(plan.items).length : applied;
    el.progressFill.style.width = `${percent}%`;
    el.downloadResult.href = `/api/jobs/${job.id}/download?file=catalog`;
    el.downloadResult.classList.toggle("is-hidden", job.status !== "verified");
    el.submitMessage.className = "message-band";
    if (job.status === "verified") {
      el.submitMessage.textContent = "最终验证通过。Excel 与 Google Play 已完全一致，可以下载结果文件。";
      el.submitMessage.classList.add("is-success");
    } else if (job.error) {
      el.submitMessage.textContent = needsOnlineRecheck()
        ? `自动上传已暂停。原因：${job.error}。下一步：返回步骤 3，点击“重新检查线上状态”；系统会跳过已经成功的项目，只继续未完成内容。`
        : `步骤 4 未完成。原因：${job.error}。请按步骤 3 的检查结果修正后再提交。`;
      el.submitMessage.classList.add("is-error");
    } else if (plan.actionable === 0) {
      el.submitMessage.textContent = "所有内容已写入，正在准备最终核对。";
      el.submitMessage.classList.add("is-success");
    } else if (job.operation === "apply_all") {
      el.submitMessage.textContent = refreshing
        ? "上一次请求结果不确定，系统正在重新读取 Google Play；已经成功的内容会跳过，其余内容会自动继续。"
        : "系统正在依次创建、更新和激活商品与首周优惠；写入结束后会自动执行一次最终核对。";
    } else {
      el.submitMessage.textContent = `确认后将自动同步全部 ${plan.actionable} 项；只有需要修改 Excel 或账号权限时才会停止并说明具体步骤。`;
    }
    el.submitMessage.classList.remove("is-hidden");
    el.submitBody.replaceChildren();
    groupSubmissionItems(plan.items).forEach((group) => {
      const fallback = fallbackPrices(group.productId);
      const originalLabel = group.oneTime ? "商品价格" : "原价";
      const originalOutcome = submissionOutcome(group.originalPrice, originalLabel, activeKeys, refreshing, job);
      const firstWeekOutcome = submissionOutcome(group.firstWeekPrice, "首周价", activeKeys, refreshing, job);
      const outcomes = [originalOutcome, firstWeekOutcome].filter(Boolean);
      const priceDetails = [
        itemPriceDetail(group.originalPrice, originalLabel, fallback.original),
        itemPriceDetail(group.firstWeekPrice, "首周价", fallback.firstWeek),
      ].filter(Boolean);
      const row = document.createElement("tr");
      appendCell(row, group.productId);
      appendCell(row, group.oneTime ? "商品价格" : "原价 + 首周价");
      const [status, style] = groupedProgress(outcomes);
      const statusCell = document.createElement("td");
      statusCell.appendChild(statusPill(status, style));
      row.appendChild(statusCell);
      const resultCell = document.createElement("td");
      resultCell.className = "submission-result";
      const result = document.createElement("strong");
      result.textContent = outcomes.map((outcome) => outcome.text).join("；");
      const prices = document.createElement("small");
      prices.textContent = priceDetails.join("；");
      resultCell.append(result, prices);
      row.appendChild(resultCell);
      el.submitBody.appendChild(row);
    });
  }

  function render() {
    const unlocked = maxUnlockedStep();
    if (state.currentStep > unlocked) state.currentStep = unlocked;
    renderNavigation();
    if (state.currentStep === 0) renderCredential();
    if (state.currentStep === 1) {
      renderDemo();
      renderExcel();
    }
    if (state.currentStep === 2) renderPlan();
    if (state.currentStep === 3) renderSubmit();
    renderFooter();
  }

  async function processCredentialSelection() {
    const file = state.selectedCredentialFile;
    const appName = state.appName;
    const profile = state.profile;
    if (!file || !appName || !profile) {
      state.credential = null;
      state.credentialReady = false;
      state.credentialChecked = false;
      render();
      saveState();
      return;
    }

    el.credentialFileTitle.textContent = file.name;
    setBadge(el.credentialState, "正在读取", "is-running");
    try {
      await validateServiceAccountFile(file, profile);
      if (
        state.selectedCredentialFile !== file ||
        state.appName !== appName ||
        state.profile !== profile
      ) return;
      const form = new FormData();
      form.set("app", appName);
      form.set("profile", profile);
      form.set("file", file);
      const credential = await api("/api/auth", { method: "POST", body: form });
      if (
        state.selectedCredentialFile !== file ||
        state.appName !== appName ||
        state.profile !== profile
      ) return;
      window.clearTimeout(state.pollTimer);
      state.credential = credential;
      state.credentialReady = true;
      state.credentialChecked = false;
      state.currentStep = state.job
        ? state.job.status === "verified" || (state.job.plan && state.job.plan.blocked === 0)
          ? 3
          : state.job.plan ? 2 : state.job.validation ? 1 : 0
        : 0;
      if (!state.job) {
        el.excelFile.value = "";
        el.excelFileTitle.textContent = "选择支付项 Excel";
      }
      saveState();
      render();
      loadRecent();
      schedulePoll();
      showToast("App 与本地服务账号匹配，可以进入下一步");
    } catch (error) {
      if (
        state.selectedCredentialFile !== file ||
        state.appName !== appName ||
        state.profile !== profile
      ) return;
      state.credential = null;
      state.credentialReady = false;
      state.credentialChecked = false;
      state.currentStep = 0;
      el.credentialFile.value = "";
      el.credentialFileTitle.textContent = file.name;
      render();
      setBadge(el.credentialState, "核对失败", "is-error");
      saveState();
      showToast(error.message);
    }
  }

  async function checkCredential() {
    if (!state.credentialReady) return;
    el.checkCredentialButton.disabled = true;
    el.checkCredentialButton.textContent = "正在检查…";
    try {
      state.credential = await api("/api/auth/check", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ app: state.appName, profile: state.profile, package: el.packageInput.value.trim() }),
      });
      state.credentialChecked = state.credential.api_access === "通过";
      render();
      saveState();
      showToast(state.credentialChecked ? "Google Play 访问检查通过" : "凭证文件有效，但 Google API 暂时未验证通过");
    } catch (error) {
      showToast(error.message);
    } finally {
      el.checkCredentialButton.textContent = "检查 Google 访问";
      el.checkCredentialButton.disabled = false;
    }
  }

  async function uploadExcel(file) {
    if (!state.credentialReady) {
      showToast("请先完成服务账号步骤");
      return;
    }
    if (isRunning()) {
      showToast("当前任务仍在运行，请完成后再重新上传");
      return;
    }
    el.excelFileTitle.textContent = file.name;
    const form = new FormData();
    form.set("file", file);
    form.set("app", state.appName);
    form.set("package", el.packageInput.value.trim());
    form.set("profile", state.profile);
    form.set("allow_one_time_price_changes", el.priceChangeInput.checked ? "true" : "false");
    try {
      state.job = await api("/api/jobs", { method: "POST", body: form });
      state.currentStep = 1;
      saveState();
      render();
      schedulePoll();
      showToast("Excel 已上传，正在本地校验");
    } catch (error) {
      showToast(error.message);
    }
  }

  async function runPlan() {
    if (!state.job?.validation || isRunning()) return;
    const previousJob = state.job;
    state.job = {
      ...state.job,
      status: "running",
      stage: "正在读取 Google 差异",
      operation: "plan",
      plan: null,
      active_batch: null,
      error: null,
      plan_revision: Number(state.job.plan_revision || 0) + 1,
    };
    state.currentStep = 2;
    render();
    saveState();
    try {
      state.job = await api(`/api/jobs/${state.job.id}/plan`, { method: "POST" });
      saveState();
      render();
      schedulePoll();
    } catch (error) {
      state.job = previousJob;
      render();
      saveState();
      showToast(`步骤 3 检查未开始：${error.message}。请确认步骤 2 已校验完成后重试。`);
    }
  }

  async function runSubmit() {
    if (!state.job?.plan || isRunning()) return;
    try {
      state.job = await api(`/api/jobs/${state.job.id}`);
      render();
      saveState();
      if (state.job.status === "verified") {
        showToast("Google Play 已核对一致，没有内容需要重复提交。");
        return;
      }
      if (isRunning()) {
        showToast(runningGuidance());
        schedulePoll();
        return;
      }
      if (!state.job.plan) {
        state.currentStep = 2;
        render();
        saveState();
        showToast("无法提交：步骤 3 的检查结果已失效，请在步骤 3 重新检查线上状态。");
        return;
      }
      const confirmation = el.confirmPackageInput.value.trim();
      if (confirmation !== state.job.package) {
        showToast("无法提交：输入的包名与目标包名不一致。请留在步骤 4，重新输入正确的完整包名。");
        return;
      }
      if (state.job.plan.actionable === 0) {
        state.job = await api(`/api/jobs/${state.job.id}/verify`, { method: "POST" });
      } else {
        state.job = await api(`/api/jobs/${state.job.id}/apply-all`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ confirm_package: confirmation }),
        });
      }
      render();
      saveState();
      schedulePoll();
    } catch (error) {
      showToast(`步骤 4 无法继续：${error.message}。请返回步骤 3 重新检查线上状态，再按检查结果处理。`);
    }
  }

  async function clearTable() {
    if (!state.job) return;
    if (isRunning()) {
      showToast("任务仍在运行，完成后才能清空");
      return;
    }
    if (!window.confirm("只清除本机 Excel、计划和表格缓存，不会删除 Google Play 商品。继续吗？")) return;
    try {
      await api(`/api/jobs/${state.job.id}/clear`, { method: "POST" });
      window.clearTimeout(state.pollTimer);
      state.job = null;
      state.currentStep = 1;
      el.excelFile.value = "";
      el.excelFileTitle.textContent = "选择支付项 Excel";
      el.confirmPackageInput.value = "";
      saveState();
      render();
      loadRecent();
      showToast("本地表格缓存已清空");
    } catch (error) {
      showToast(error.message);
    }
  }

  function mergeJobStatus(progress) {
    const plan = progress.plan && state.job?.plan
      ? { ...state.job.plan, ...progress.plan }
      : progress.plan;
    state.job = { ...state.job, ...progress, plan };
  }

  function schedulePoll(delay) {
    window.clearTimeout(state.pollTimer);
    if (!state.job || !isRunning()) return;
    state.pollTimer = window.setTimeout(async () => {
      try {
        let progress = null;
        if (state.lightweightStatusSupported) {
          try {
            const revision = Number(state.job.state_revision || 0);
            progress = await api(`/api/jobs/${state.job.id}/status?after=${revision}`);
          } catch (error) {
            if (error.status !== 404) throw error;
            state.lightweightStatusSupported = false;
          }
        }
        if (!progress) {
          state.job = await api(`/api/jobs/${state.job.id}`);
        } else if (Number(progress.plan_revision || 0) !== Number(state.job.plan_revision || 0)) {
          state.job = await api(`/api/jobs/${state.job.id}`);
        } else {
          mergeJobStatus(progress);
        }
        saveState();
        render();
        if (isRunning()) schedulePoll();
        else loadRecent();
      } catch (error) {
        showToast(error.message);
        schedulePoll(5000);
      }
    }, delay ?? (state.lightweightStatusSupported ? 100 : document.hidden ? 30000 : 10000));
  }

  function recentStep(job) {
    if (job.status === "verified") return "验证完成";
    if (job.operation === "apply_all") return "正在自动提交全部";
    if (job.operation === "apply") return "正在同步";
    if (job.operation === "plan") return "正在检查差异";
    if (job.operation === "prepare") return "正在校验 Excel";
    if (job.plan?.blocked) return `阻塞 ${job.plan.blocked} 项`;
    if (job.plan) return `待提交 ${job.plan.actionable} 项`;
    if (job.validation) return "Excel 已校验";
    return job.stage || "等待处理";
  }

  function recentCounts(job) {
    const total = Number(job.plan?.total || 0);
    const remaining = Number(job.plan?.actionable || 0);
    const successful = Number(job.applied_total || 0);
    if (job.status === "verified") return `${total} 项已核对一致`;
    if (job.plan?.blocked) return `${job.plan.blocked} 项阻塞`;
    if (job.operation === "apply_all" || job.operation === "apply") return `${successful} 已完成 / ${remaining} 待处理`;
    if (job.plan) return `${total} 已检查 / ${remaining} 待处理`;
    return "等待生成检查结果";
  }

  async function loadRecent() {
    try {
      const result = await api("/api/jobs");
      el.recentBody.replaceChildren();
      el.recentEmpty.classList.toggle("is-hidden", result.jobs.length > 0);
      el.recentTableWrap.classList.toggle("is-hidden", result.jobs.length === 0);
      result.jobs.forEach((job) => {
        const row = document.createElement("tr");
        appendCell(row, job.original_name);
        appendCell(row, `${appForProfile(job.profile) || "未命名 App"} · ${job.package}`);
        const progress = document.createElement("td");
        const style = job.status === "verified" ? "is-success" : job.plan?.blocked || ["error", "uncertain"].includes(job.status) ? "is-error" : job.operation ? "is-blue" : "is-warning";
        progress.appendChild(statusPill(recentStep(job), style));
        row.appendChild(progress);
        appendCell(row, recentCounts(job));
        appendCell(row, formatDate(job.updated_at));
        const action = document.createElement("td");
        const button = document.createElement("button");
        button.type = "button";
        button.className = "link-button";
        button.textContent = "打开 →";
        button.addEventListener("click", () => openRecent(job.id));
        action.appendChild(button);
        row.appendChild(action);
        el.recentBody.appendChild(row);
      });
    } catch (error) {
      showToast(error.message);
    }
  }

  async function openRecent(jobId) {
    if (jobId !== state.job?.id && !confirmDisruptiveAction("打开另一条任务")) return;
    try {
      const job = await api(`/api/jobs/${jobId}`);
      const appName = appForProfile(job.profile);
      if (!appName) throw new Error(`apps.json 中没有 profile：${job.profile}`);
      state.appName = appName;
      state.profile = job.profile;
      state.credential = null;
      state.credentialReady = false;
      state.credentialChecked = false;
      state.job = job;
      el.appSelect.value = appName;
      el.packageInput.value = job.package;
      const saved = restoreState();
      el.confirmPackageInput.value = saved.confirmationJobId === job.id
        ? saved.confirmPackage || ""
        : "";
      state.currentStep = 0;
      saveState();
      render();
      await processCredentialSelection();
      window.scrollTo({ top: 0, behavior: "smooth" });
    } catch (error) {
      showToast(error.message);
    }
  }

  el.appSelect.addEventListener("change", async () => {
    const nextApp = el.appSelect.value;
    if (nextApp === state.appName) return;
    if (!confirmDisruptiveAction("切换 App")) {
      el.appSelect.value = state.appName;
      return;
    }
    state.appName = nextApp;
    state.profile = state.apps[nextApp]?.name || "";
    el.packageInput.value = state.apps[nextApp]?.pack || "";
    state.credential = null;
    state.credentialReady = false;
    state.credentialChecked = false;
    state.job = null;
    window.clearTimeout(state.pollTimer);
    state.currentStep = 0;
    el.credentialFileTitle.textContent = state.selectedCredentialFile?.name || "选择 Google 凭证文件";
    saveState();
    render();
    await processCredentialSelection();
  });
  el.credentialFile.addEventListener("change", async () => {
    const file = el.credentialFile.files[0];
    if (!file) return;
    if (!confirmDisruptiveAction("替换服务账号")) {
      el.credentialFile.value = "";
      return;
    }
    state.selectedCredentialFile = file;
    state.credential = null;
    state.credentialReady = false;
    state.credentialChecked = false;
    state.currentStep = 0;
    el.credentialFileTitle.textContent = file.name;
    saveState();
    render();
    await processCredentialSelection();
  });
  el.checkCredentialButton.addEventListener("click", checkCredential);
  el.excelFile.addEventListener("change", () => {
    const file = el.excelFile.files[0];
    if (file) uploadExcel(file);
  });
  el.runPlanButton.addEventListener("click", runPlan);
  el.confirmPackageInput.addEventListener("input", () => {
    renderFooter();
    saveState();
  });
  el.backButton.addEventListener("click", () => goToStep(state.currentStep - 1));
  el.nextButton.addEventListener("click", () => goToStep(state.currentStep + 1));
  el.submitButton.addEventListener("click", runSubmit);
  $("#help-button").addEventListener("click", () => el.helpDialog.showModal());
  $("#close-help-button").addEventListener("click", () => el.helpDialog.close());
  $("#understand-help-button").addEventListener("click", () => {
    localStorage.setItem(HELP_STORAGE_KEY, "seen");
    el.helpDialog.close();
  });
  el.helpDialog.addEventListener("click", (event) => {
    if (event.target === el.helpDialog) el.helpDialog.close();
  });
  $$(".step-nav__item").forEach((button) => button.addEventListener("click", () => goToStep(Number(button.dataset.step))));
  $$(".clear-table-button").forEach((button) => button.addEventListener("click", clearTable));
  $("#refresh-jobs").addEventListener("click", loadRecent);

  async function init() {
    try {
      localStorage.removeItem(PREVIOUS_STORAGE_KEY);
      state.config = await api("/api/config");
      state.lightweightStatusSupported = Boolean(state.config.lightweight_job_status);
      const [appsResult, demo] = await Promise.all([api("/api/apps"), api("/api/demo")]);
      state.apps = appsResult.apps;
      state.demo = demo;
      el.packageInput.value = "";
      el.appSelect.replaceChildren();
      const prompt = document.createElement("option");
      prompt.value = "";
      prompt.textContent = "请选择应用";
      el.appSelect.appendChild(prompt);
      Object.entries(state.apps).forEach(([app, config]) => {
        const option = document.createElement("option");
        option.value = app;
        option.textContent = `${app} (${config.name})`;
        el.appSelect.appendChild(option);
      });

      state.currentStep = 0;
      render();
      schedulePoll();
      await loadRecent();
      if (!localStorage.getItem(HELP_STORAGE_KEY)) el.helpDialog.showModal();
    } catch (error) {
      showToast(error.message);
    }
  }

  init();
}
