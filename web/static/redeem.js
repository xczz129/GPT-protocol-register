(function () {
  const panelView = location.hash.replace(/^#/, "");
  if (["task", "fingerprint", "mail", "proxy", "chatgpt2api", "logs", "accounts", "link-center", "cdk", "operations"].includes(panelView)) {
    location.replace(`/?panel=1&t=${Date.now()}#${panelView}`);
    return;
  }
  const queryMode = location.pathname === "/cdk-query";
  const gate = document.getElementById("redeemGate");
  const queryGate = document.getElementById("queryGate");
  const inbox = document.getElementById("inbox");
  const form = document.getElementById("redeemForm");
  const queryForm = document.getElementById("queryForm");
  const input = document.getElementById("cdkInput");
  const queryInput = document.getElementById("queryInput");
  const redeemButton = document.getElementById("redeemButton");
  const queryButton = document.getElementById("queryButton");
  const redeemMessage = document.getElementById("redeemMessage");
  const queryMessage = document.getElementById("queryMessage");
  const queryResult = document.getElementById("queryResult");
  const redeemCodeCount = document.getElementById("redeemCodeCount");
  const batchResult = document.getElementById("batchResult");
  const batchSummary = document.getElementById("batchSummary");
  const batchResultList = document.getElementById("batchResultList");
  const mailboxAddress = document.getElementById("mailboxAddress");
  const sessionState = document.getElementById("sessionState");
  const messageList = document.getElementById("messageList");
  const emptyInbox = document.getElementById("emptyInbox");
  const messageCount = document.getElementById("messageCount");
  const latestCode = document.getElementById("latestCode");
  const accountPassword = document.getElementById("accountPassword");
  const totpSecret = document.getElementById("totpSecret");
  const mailStatus = document.getElementById("mailStatus");
  const mailNotice = document.getElementById("mailNotice");
  const refreshMail = document.getElementById("refreshMail");
  const copyAccessToken = document.getElementById("copyAccessToken");
  const downloadAccountJson = document.getElementById("downloadAccountJson");
  const redeemModeLink = document.getElementById("redeemModeLink");
  const queryModeLink = document.getElementById("queryModeLink");
  let mailbox = "";
  let loadingMessages = false;
  let refreshTimer = null;
  let noticeTimer = null;
  let batchResults = [];

  redeemModeLink.classList.toggle("active", !queryMode);
  redeemModeLink.setAttribute("aria-current", queryMode ? "false" : "page");
  queryModeLink.classList.toggle("active", queryMode);
  queryModeLink.setAttribute("aria-current", queryMode ? "page" : "false");

  function svgIcon(name, className) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    if (className) svg.setAttribute("class", className);
    svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", `#icon-${name}`);
    svg.appendChild(use);
    return svg;
  }

  function setButtonLoading(button, loading, busyLabel, idleLabel) {
    button.disabled = loading;
    button.classList.toggle("loading", loading);
    button.setAttribute("aria-busy", loading ? "true" : "false");
    const label = button.querySelector("span");
    if (label) label.textContent = loading ? busyLabel : idleLabel;
    const use = button.querySelector("use");
    if (use) use.setAttribute("href", loading ? "#icon-refresh" : "#icon-arrow-right");
  }

  async function request(path, options, timeoutMs = 15000) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(path, {
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        ...(options || {}),
        signal: controller.signal,
      });
      let data = null;
      try { data = await response.json(); } catch {}
      if (!response.ok) throw new Error((data && (data.detail || data.error)) || `HTTP ${response.status}`);
      return data || {};
    } catch (error) {
      if (error && error.name === "AbortError") throw new Error("请求超时，请重试");
      throw error;
    } finally {
      clearTimeout(timeout);
    }
  }

  function announce(message, error) {
    clearTimeout(noticeTimer);
    mailNotice.textContent = message || "";
    mailNotice.className = `mail-notice${error ? " error" : ""}`;
    if (message && !error) {
      noticeTimer = setTimeout(() => { mailNotice.textContent = ""; }, 3200);
    }
  }

  async function copy(value, label) {
    if (!value) return;
    try {
      await navigator.clipboard.writeText(value);
    } catch {
      const helper = document.createElement("textarea");
      helper.value = value;
      helper.style.position = "fixed";
      helper.style.opacity = "0";
      document.body.appendChild(helper);
      helper.select();
      document.execCommand("copy");
      helper.remove();
    }
    announce(`${label}已复制`, false);
  }

  function setFormMessage(target, message, error) {
    target.textContent = message || "";
    target.className = `form-message${error ? " error" : ""}`;
  }

  function parseRedeemCodes() {
    return Array.from(new Set(
      String(input.value || "")
        .toUpperCase()
        .split(/[\s,;，；]+/)
        .map((value) => value.trim())
        .filter(Boolean)
    ));
  }

  function redeemIdleLabel(count = parseRedeemCodes().length) {
    if (count > 20) return "兑换前 20 个 CDK";
    if (count > 1) return `兑换 ${count} 个 CDK`;
    return "兑换并进入";
  }

  function updateRedeemInput() {
    const count = parseRedeemCodes().length;
    redeemCodeCount.textContent = count > 20 ? `识别到 ${count} 个，仅处理前 20 个` : count ? `已识别 ${count} 个` : "等待输入";
    const label = redeemButton.querySelector("span");
    if (label && redeemButton.getAttribute("aria-busy") !== "true") label.textContent = redeemIdleLabel(count);
    redeemCodeCount.hidden = count === 0;
    input.style.height = "64px";
    input.style.height = `${Math.min(192, Math.max(64, input.scrollHeight))}px`;
  }

  function showGate(message, error) {
    gate.hidden = queryMode;
    queryGate.hidden = !queryMode;
    inbox.hidden = true;
    document.title = `${queryMode ? "CDK 查询" : "CDK 兑换"} · XBOVO Mail`;
    sessionState.innerHTML = `<i></i>${queryMode ? "查询入口" : "兑换入口"}`;
    accountPassword.textContent = "未返回";
    accountPassword.disabled = true;
    accountPassword.dataset.value = "";
    accountPassword.title = "暂无密码可复制";
    totpSecret.textContent = "未返回";
    totpSecret.disabled = true;
    totpSecret.dataset.value = "";
    totpSecret.title = "暂无 TOTP 密钥可复制";
    setFormMessage(queryMode ? queryMessage : redeemMessage, message, error);
    clearInterval(refreshTimer);
    refreshTimer = null;
    setTimeout(() => (queryMode ? queryInput : input).focus(), 0);
  }

  function showInbox(session) {
    mailbox = session.email || "";
    gate.hidden = true;
    queryGate.hidden = true;
    inbox.hidden = false;
    mailboxAddress.textContent = mailbox || "未知邮箱";
    const password = String(session.password || "");
    const secret = String(session.totp_secret || "");
    accountPassword.dataset.value = password;
    accountPassword.textContent = password || (session.password_set ? "已设置" : "未设置");
    accountPassword.disabled = !password;
    accountPassword.title = password ? "点击复制密码" : "暂无密码可复制";
    totpSecret.dataset.value = secret;
    totpSecret.textContent = secret || (session.mfa_enabled ? "已启用" : "未启用");
    totpSecret.disabled = !secret;
    totpSecret.title = secret ? "点击复制 TOTP 密钥" : "暂无 TOTP 密钥可复制";
    sessionState.innerHTML = "<i></i>收件箱已连接";
    document.title = `${mailbox || "收件箱"} · XBOVO Mail`;
    scheduleRefresh();
  }

  function showQueryResult(data) {
    const ready = data.status === "redeemed";
    const icon = document.createElement("span");
    icon.className = `query-status-icon${ready ? "" : " pending"}`;
    icon.appendChild(svgIcon(ready ? "check" : "alert"));

    const copy = document.createElement("span");
    copy.className = "query-copy";
    const title = document.createElement("b");
    title.textContent = data.status_label || data.status || "未知状态";
    const email = document.createElement("span");
    email.textContent = data.email || "尚未绑定邮箱";
    copy.append(title, email);

    const time = document.createElement("time");
    time.className = "query-time";
    time.textContent = data.redeemed_at ? formatTime(data.redeemed_at) : "等待兑换";
    queryResult.replaceChildren(icon, copy, time);
    queryResult.hidden = false;
  }

  function scheduleRefresh() {
    clearInterval(refreshTimer);
    refreshTimer = setInterval(() => {
      if (!document.hidden) loadMessages(false).catch(() => {});
    }, 5000);
  }

  function formatTime(value) {
    if (!value) return "";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
  }

  function senderInitial(sender) {
    const clean = String(sender || "").trim();
    if (!clean) return "?";
    const address = clean.match(/[A-Z0-9._%+-]+@/i)?.[0] || clean;
    return address.charAt(0).toUpperCase();
  }

  function renderMessages(items) {
    messageList.replaceChildren();
    messageCount.textContent = String(items.length);
    const newestCode = items.find((item) => item.code)?.code || "";
    latestCode.textContent = newestCode || "等待邮件";
    latestCode.disabled = !newestCode;
    latestCode.dataset.code = newestCode;
    emptyInbox.hidden = items.length !== 0;

    for (const item of items) {
      const details = document.createElement("details");
      details.className = "mail-row";
      const summary = document.createElement("summary");
      summary.className = "mail-summary";

      const senderMark = document.createElement("span");
      senderMark.className = "sender-mark";
      senderMark.textContent = senderInitial(item.sender);

      const title = document.createElement("div");
      title.className = "mail-title";
      const subject = document.createElement("b");
      subject.textContent = item.subject || "无主题";
      const sender = document.createElement("span");
      sender.textContent = item.sender || "未知发件人";
      title.append(subject, sender);

      const meta = document.createElement("div");
      meta.className = "mail-meta";
      if (item.code) {
        const code = document.createElement("button");
        code.type = "button";
        code.className = "code-chip";
        code.textContent = item.code;
        code.title = "复制验证码";
        code.addEventListener("click", (event) => {
          event.preventDefault();
          event.stopPropagation();
          copy(item.code, "验证码").catch(() => {});
        });
        meta.appendChild(code);
      }
      const time = document.createElement("time");
      time.textContent = formatTime(item.received_at);
      meta.appendChild(time);

      summary.append(senderMark, title, meta, svgIcon("chevron", "row-chevron"));
      const body = document.createElement("pre");
      body.className = "mail-body";
      body.textContent = item.text || item.preview || "邮件正文为空";
      details.append(summary, body);

      if (item.has_raw && item.id) {
        details.addEventListener("toggle", async () => {
          if (!details.open || details.dataset.loaded || details.dataset.loading) return;
          details.dataset.loading = "1";
          body.classList.add("loading");
          body.textContent = "正在读取完整正文…";
          try {
            const data = await request("/api/redeem/message", {
              method: "POST",
              body: JSON.stringify({ message_id: String(item.id) }),
            }, 20000);
            const message = data.item || {};
            body.textContent = message.text || message.preview || "邮件正文为空";
            details.dataset.loaded = "1";
          } catch (error) {
            body.textContent = error.message || "邮件正文读取失败";
          } finally {
            delete details.dataset.loading;
            body.classList.remove("loading");
          }
        });
      }
      messageList.appendChild(details);
    }
  }

  async function openBatchMailbox(result, button) {
    button.disabled = true;
    try {
      const session = await request("/api/cdk-query", {
        method: "POST",
        body: JSON.stringify({ code: result.code }),
      });
      if (!session.can_read_mail) throw new Error("该 CDK 尚未完成兑换");
      showInbox(session);
      loadMessages(true).catch(() => {});
    } catch (error) {
      setFormMessage(redeemMessage, error.message || "打开收件箱失败", true);
    } finally {
      button.disabled = false;
    }
  }

  function renderBatchResults() {
    batchResultList.replaceChildren();
    const successes = batchResults.filter((item) => item.ok).length;
    batchSummary.textContent = `成功 ${successes} · 失败 ${batchResults.length - successes}`;
    batchResult.hidden = !batchResults.length;

    for (const result of batchResults) {
      const row = document.createElement("article");
      row.className = "batch-result-row";
      const code = document.createElement("code");
      code.textContent = result.code;
      code.title = result.code;
      const detail = document.createElement("span");
      detail.className = "result-email";
      detail.textContent = result.email || result.error || "未返回邮箱";
      if (result.ok) {
        detail.textContent += ` · 密码 ${result.password_set ? "已设置" : "缺失"} · 2FA ${result.mfa_enabled ? (result.mfa_factor_type || "已启用") : "未启用"}`;
      }
      detail.title = detail.textContent;
      const status = document.createElement("span");
      status.className = `result-status${result.ok ? "" : " failed"}`;
      status.append(svgIcon(result.ok ? "check" : "alert"));
      const label = document.createElement("span");
      label.textContent = result.ok ? "兑换成功" : result.queryable ? "已兑换" : "兑换失败";
      status.appendChild(label);
      row.append(code, detail, status);
      if (result.ok || result.queryable) {
        const open = document.createElement("button");
        open.type = "button";
        open.className = "batch-open";
        open.append(document.createTextNode("打开"), svgIcon("arrow-right"));
        open.addEventListener("click", () => openBatchMailbox(result, open));
        row.appendChild(open);
      } else {
        row.appendChild(document.createElement("span"));
      }
      batchResultList.appendChild(row);
    }
  }

  async function redeemBatch(codes) {
    batchResults = [];
    renderBatchResults();
    setButtonLoading(redeemButton, true, "正在批量兑换", redeemIdleLabel(codes.length));
    try {
      setFormMessage(redeemMessage, `正在兑换 0 / ${codes.length}`, false);
      const data = await request("/api/redeem/batch", {
        method: "POST",
        body: JSON.stringify({ codes }),
      }, 30000);
      batchResults = Array.isArray(data.items) ? data.items : [];
      renderBatchResults();
      const successes = batchResults.filter((item) => item.ok).length;
      const message = `批量兑换完成：成功 ${successes}，失败 ${batchResults.length - successes}`;
      setFormMessage(redeemMessage, message, successes === 0);
    } catch (error) {
      setFormMessage(redeemMessage, error.message || "批量兑换失败", true);
    } finally {
      setButtonLoading(redeemButton, false, "正在批量兑换", redeemIdleLabel());
    }
  }

  async function loadMessages(notify) {
    if (loadingMessages) return;
    loadingMessages = true;
    refreshMail.disabled = true;
    refreshMail.classList.add("loading");
    refreshMail.setAttribute("aria-busy", "true");
    mailStatus.textContent = "同步中";
    if (notify) announce("正在读取邮件…", false);
    try {
      const data = await request("/api/redeem/messages", {
        method: "POST",
        body: JSON.stringify({ limit: 30 }),
      }, 30000);
      if (data.ok === false) throw new Error(data.error || "邮件读取失败");
      renderMessages(data.items || []);
      mailStatus.textContent = "已同步";
      if (notify) announce("邮件已刷新", false);
    } catch (error) {
      if (/401|会话/.test(error.message || "")) {
        showGate("查询会话已失效，请重新输入 CDK", true);
      } else {
        mailStatus.textContent = "同步异常";
        announce(error.message || "邮件读取失败", true);
      }
    } finally {
      loadingMessages = false;
      refreshMail.disabled = false;
      refreshMail.classList.remove("loading");
      refreshMail.setAttribute("aria-busy", "false");
    }
  }

  function normalizeInput(event) {
    const element = event.currentTarget;
    const cursor = element.selectionStart;
    element.value = element.value.toUpperCase().replace(/\s+/g, "");
    if (cursor !== null) element.setSelectionRange(cursor, cursor);
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const allCodes = parseRedeemCodes();
    if (!allCodes.length) return;
    if (allCodes.length > 1) {
      await redeemBatch(allCodes.slice(0, 20));
      return;
    }
    batchResults = [];
    renderBatchResults();
    const code = allCodes[0];
    setButtonLoading(redeemButton, true, "正在兑换", "兑换并进入");
    setFormMessage(redeemMessage, "正在验证 CDK…", false);
    try {
      const session = await request("/api/redeem", {
        method: "POST",
        body: JSON.stringify({ code }),
      });
      input.value = "";
      updateRedeemInput();
      showInbox(session);
      loadMessages(true).catch(() => {});
    } catch (error) {
      showGate(error.message || "兑换失败", true);
    } finally {
      setButtonLoading(redeemButton, false, "正在兑换", "兑换并进入");
    }
  });

  queryForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const code = (queryInput.value || "").trim().toUpperCase();
    if (!code) return;
    queryResult.hidden = true;
    setButtonLoading(queryButton, true, "正在查询", "查询记录");
    setFormMessage(queryMessage, "正在读取兑换记录…", false);
    try {
      const result = await request("/api/cdk-query", {
        method: "POST",
        body: JSON.stringify({ code }),
      });
      if (result.can_read_mail) {
        queryInput.value = "";
        showInbox(result);
        loadMessages(true).catch(() => {});
      } else {
        showQueryResult(result);
        setFormMessage(queryMessage, "该 CDK 尚未兑换", false);
      }
    } catch (error) {
      showGate(error.message || "查询失败", true);
    } finally {
      setButtonLoading(queryButton, false, "正在查询", "查询记录");
    }
  });

  input.addEventListener("input", () => {
    input.value = input.value.toUpperCase();
    updateRedeemInput();
  });
  queryInput.addEventListener("input", normalizeInput);
  document.getElementById("copyEmail").addEventListener("click", () => copy(mailbox, "邮箱").catch(() => {}));
  accountPassword.addEventListener("click", () => copy(accountPassword.dataset.value || "", "密码").catch(() => {}));
  totpSecret.addEventListener("click", () => copy(totpSecret.dataset.value || "", "TOTP 密钥").catch(() => {}));
  copyAccessToken.addEventListener("click", async () => {
    copyAccessToken.disabled = true;
    try {
      const data = await request("/api/redeem/access-token", { method: "GET" });
      await copy(data.access_token || "", "AT");
    } catch (error) {
      announce(error.message || "AT 读取失败", true);
    } finally {
      copyAccessToken.disabled = false;
    }
  });
  downloadAccountJson.addEventListener("click", () => {
    window.location.assign(`/api/redeem/account.json?t=${Date.now()}`);
  });
  refreshMail.addEventListener("click", () => loadMessages(true));
  document.getElementById("emptyRefresh").addEventListener("click", () => loadMessages(true));
  latestCode.addEventListener("click", () => copy(latestCode.dataset.code || "", "验证码").catch(() => {}));
  document.getElementById("leaveInbox").addEventListener("click", async () => {
    try { await request("/api/redeem/logout", { method: "POST", body: "{}" }); } catch {}
    mailbox = "";
    messageList.replaceChildren();
    showGate("", false);
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && !inbox.hidden) loadMessages(false).catch(() => {});
  });

  updateRedeemInput();
  showGate("", false);
  request("/api/redeem/session", { method: "GET" })
    .then((session) => {
      if (!session.authenticated) return;
      showInbox(session);
      loadMessages(false).catch(() => {});
    })
    .catch(() => {});
})();
