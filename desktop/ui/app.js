const invoke = (cmd, args = {}) => window.__TAURI__.core.invoke(cmd, args);
const dialog = window.__TAURI__.dialog;

let currentLogTarget = "ai";
let currentHistoryFilter = "all";
let rawLogText = "";
let toastTimer = null;

const TYPE_LABELS = {
  single: "单选",
  multiple: "多选",
  judgement: "判断",
  completion: "填空",
  vote: "投票",
};

function showToast(msg) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 2800);
}

function escapeHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

// ── 主题切换 ──────────────────────────────────────────────────────
function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  localStorage.setItem("ai-tiku-theme", theme);
  document.getElementById("theme-label").textContent =
    theme === "dark" ? "切换浅色主题" : "切换深色主题";
}
applyTheme(localStorage.getItem("ai-tiku-theme") || "dark");
document.getElementById("btn-theme").addEventListener("click", () => {
  const cur = document.documentElement.getAttribute("data-theme") || "dark";
  applyTheme(cur === "dark" ? "light" : "dark");
});

// ── 侧边栏导航 ───────────────────────────────────────────────────
document.querySelectorAll(".nav-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".nav-btn").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById(`tab-${btn.dataset.tab}`).classList.add("active");
    if (btn.dataset.tab === "history") loadHistory();
    if (btn.dataset.tab === "settings") loadEnvConfig();
    if (btn.dataset.tab === "ocs") loadOcsPreview();
  });
});

// ── 总控台状态与来源占比 ─────────────────────────────────────────
async function refreshStatus() {
  try {
    const s = await invoke("get_status");
    document.getElementById("project-root-label").textContent = `项目目录：${s.project_root}`;
    document.getElementById("dot-ai").className = `dot ${s.ai_running ? "online" : "offline"}`;
    document.getElementById("dot-tiku").className = `dot ${s.tiku_running ? "online" : "offline"}`;
    document.getElementById("status-ai-text").textContent = s.ai_running ? "运行中" : "已停止";
    document.getElementById("status-tiku-text").textContent = s.tiku_running ? "运行中" : "已停止";
    document.getElementById("stat-bank").textContent = `${s.answers_count} 题`;

    const total = s.history_count || 0;
    const okCount = Math.max(0, total - s.error_count);
    const okRate = total > 0 ? Math.round((okCount / total) * 100) : 100;
    document.getElementById("stat-history").textContent = `${total} 次 · ${okRate}%`;

    const saved = (s.breakdown?.local || 0) + (s.breakdown?.cache || 0);
    const saveRate = total > 0 ? Math.round((saved / total) * 100) : 0;
    document.getElementById("stat-save-rate").textContent = `缓存免付费命中 ${saveRate}%`;

    const c = s.cost || {};
    const todayCost = Number(c.today_cost_rmb || 0);
    const totalCost = Number(c.total_cost_rmb || 0);
    const totalCalls = Number(c.total_calls || 0);
    const pTok = Number(c.total_prompt_tokens || 0);
    const cTok = Number(c.total_completion_tokens || 0);
    document.getElementById("cost-today").textContent = `¥${todayCost.toFixed(4)}`;
    document.getElementById("cost-today-tok").textContent = `${Number(c.today_tokens || 0).toLocaleString()} tokens`;
    document.getElementById("cost-total").textContent = `¥${totalCost.toFixed(4)}`;
    document.getElementById("cost-calls").textContent = `${totalCalls} 次 AI 调用`;
    document.getElementById("cost-tokens").textContent = `${pTok.toLocaleString()} / ${cTok.toLocaleString()}`;
    document.getElementById("cost-avg").textContent =
      totalCalls > 0 ? `均价 ¥${(totalCost / totalCalls).toFixed(4)} / 次` : "均价 - / 次";

    const errBadge = document.getElementById("badge-errors");
    if (s.error_count > 0) {
      errBadge.textContent = s.error_count;
      errBadge.classList.remove("hidden");
    } else {
      errBadge.classList.add("hidden");
    }

    const b = s.breakdown || { local: 0, cache: 0, bank_ai: 0, ai: 0, error: 0 };
    document.getElementById("cnt-local").textContent = b.local;
    document.getElementById("cnt-cache").textContent = b.cache;
    document.getElementById("cnt-bank").textContent = b.bank_ai;
    document.getElementById("cnt-ai").textContent = b.ai;
    document.getElementById("cnt-err").textContent = b.error;

    const sum = b.local + b.cache + b.bank_ai + b.ai + b.error || 1;
    const bar = document.getElementById("breakdown-bar");
    bar.innerHTML = [
      { cls: "c-local", val: b.local },
      { cls: "c-cache", val: b.cache },
      { cls: "c-bank", val: b.bank_ai },
      { cls: "c-ai", val: b.ai },
      { cls: "c-err", val: b.error },
    ]
      .filter((item) => item.val > 0)
      .map(
        (item) =>
          `<div class="progress-seg ${item.cls}" style="width:${(item.val / sum) * 100}%"></div>`
      )
      .join("");
  } catch (e) {
    console.error(e);
  }
}

// ── 实时日志终端 ─────────────────────────────────────────────────
function renderLogLines() {
  const container = document.getElementById("log-content");
  const keyword = document.getElementById("log-filter").value.trim().toLowerCase();
  const lines = (rawLogText || "").split("\n").filter((ln) => {
    if (!keyword) return true;
    return ln.toLowerCase().includes(keyword);
  });
  if (!lines.length || (lines.length === 1 && !lines[0])) {
    container.innerHTML = `<div class="log-line">（暂无匹配日志）</div>`;
    return;
  }
  const atBottom =
    container.scrollHeight - container.scrollTop - container.clientHeight < 40;
  container.innerHTML = lines
    .map((line) => {
      let cls = "";
      if (line.includes("[ERROR]") || line.includes("失败") || line.includes("Error")) cls = "err";
      else if (line.includes("[SUCCESS]") || line.includes("命中") || line.includes("一致")) cls = "ok";
      else if (line.includes("新的请求") || line.includes("不一致")) cls = "warn";
      else if (line.includes("[INFO]")) cls = "info";
      return `<div class="log-line ${cls}">${escapeHtml(line)}</div>`;
    })
    .join("");
  if (atBottom) container.scrollTop = container.scrollHeight;
}

async function loadLogs() {
  try {
    rawLogText = await invoke("read_logs", { target: currentLogTarget, lines: 160 });
    renderLogLines();
  } catch (e) {
    rawLogText = `读取日志失败: ${e}`;
    renderLogLines();
  }
}

document.querySelectorAll(".chip[data-log]").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".chip[data-log]").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    currentLogTarget = btn.dataset.log;
    loadLogs();
  });
});
document.getElementById("log-filter").addEventListener("input", renderLogLines);
document.getElementById("btn-refresh-log").addEventListener("click", loadLogs);
document.getElementById("btn-clear-log").addEventListener("click", async () => {
  const msg = await invoke("clear_logs", { target: currentLogTarget });
  showToast(msg);
  await loadLogs();
});

// ── 服务控制按钮 ─────────────────────────────────────────────────
document.getElementById("btn-start").addEventListener("click", async () => {
  showToast("正在启动服务...");
  try {
    showToast(await invoke("start_services"));
  } catch (e) {
    showToast(`启动失败: ${e}`);
  }
  await refreshStatus();
  await loadLogs();
});

document.getElementById("btn-stop").addEventListener("click", async () => {
  try {
    showToast(await invoke("stop_services"));
  } catch (e) {
    showToast(`停止失败: ${e}`);
  }
  await refreshStatus();
});

document.getElementById("btn-restart").addEventListener("click", async () => {
  showToast("正在重启服务...");
  try {
    await invoke("stop_services");
    showToast(await invoke("start_services"));
  } catch (e) {
    showToast(`重启失败: ${e}`);
  }
  await refreshStatus();
  await loadLogs();
});

// ── 作答记录与错题 ───────────────────────────────────────────────
async function loadHistory() {
  const query = document.getElementById("history-search").value.trim();
  const rows = await invoke("list_history", {
    limit: 80,
    filterPath: currentHistoryFilter,
    query,
  });
  const tbody = document.getElementById("history-tbody");
  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="muted" style="text-align:center;padding:24px">暂无匹配记录</td></tr>`;
    return;
  }
  tbody.innerHTML = rows
    .map((r, idx) => {
      const badgeCls = r.path.startsWith("bank") ? "bank" : r.path;
      const optHtml = r.options
        ? `<div class="opt-preview">${escapeHtml(r.options)}</div>`
        : "";
      return `
        <tr>
          <td>${escapeHtml(r.time_str)}</td>
          <td><span class="badge ${escapeHtml(badgeCls)}">${escapeHtml(r.path)}</span></td>
          <td>${escapeHtml(TYPE_LABELS[r.q_type] || r.q_type)}</td>
          <td><div>${escapeHtml(r.title)}</div>${optHtml}</td>
          <td><b>${escapeHtml(r.answer)}</b></td>
          <td class="muted">${escapeHtml(r.analysis)}</td>
          <td>
            <div style="display:flex;gap:6px">
              <button class="btn btn-sm btn-ghost" data-fix-idx="${idx}">修正</button>
              <button class="btn btn-sm btn-danger" data-del-idx="${idx}">删除</button>
            </div>
          </td>
        </tr>`;
    })
    .join("");
  tbody.querySelectorAll("[data-fix-idx]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const row = rows[Number(btn.dataset.fixIdx)];
      openFixModal({
        heading: "修正答案并清理旧缓存",
        title: row.title,
        options: row.options,
        q_type: row.q_type || "single",
        answer_text: row.answer === "-" ? "" : row.answer,
        analysis: row.path === "error" ? "" : row.analysis,
      });
    });
  });
  tbody.querySelectorAll("[data-del-idx]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const row = rows[Number(btn.dataset.delIdx)];
      const msg = await invoke("delete_history_item", { id: row.id, title: row.title });
      showToast(msg);
      await loadHistory();
      await refreshStatus();
    });
  });
}

document.querySelectorAll("#history-filter-seg .seg-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("#history-filter-seg .seg-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    currentHistoryFilter = btn.dataset.filter;
    loadHistory();
  });
});
document.getElementById("history-search").addEventListener("input", loadHistory);
document.getElementById("btn-refresh-history").addEventListener("click", loadHistory);

document.getElementById("btn-import-csv").addEventListener("click", async () => {
  const selected = await dialog.open({
    multiple: false,
    filters: [{ name: "题库文件", extensions: ["csv", "json"] }],
  });
  if (!selected) return;
  try {
    showToast(await invoke("import_bank_file", { path: selected }));
    await refreshStatus();
  } catch (e) {
    showToast(`导入失败: ${e}`);
  }
});

async function refreshBalance(silent = true) {
  try {
    const b = await invoke("get_api_balance");
    const el = document.getElementById("api-balance");
    const sub = document.getElementById("api-balance-sub");
    if (!b.supported) {
      el.textContent = "-";
      el.className = "cost-val";
      sub.textContent = b.message || "不支持查询";
      return;
    }
    const sym = b.currency === "USD" ? "$" : "¥";
    const num = parseFloat(b.total_balance || "0");
    el.textContent = `${sym}${b.total_balance}`;
    if (!b.is_available || num < 2.0) {
      el.className = "cost-val low";
      sub.textContent = !b.is_available ? "余额不足，请充值" : `赠金 ${b.granted_balance} · 余额偏低`;
    } else {
      el.className = "cost-val ok";
      sub.textContent = `充值 ${b.topped_up_balance} · 赠金 ${b.granted_balance}`;
    }
    if (!silent) showToast(`当前 DeepSeek 余额：${sym}${b.total_balance}`);
  } catch (e) {
    document.getElementById("api-balance").textContent = "查询失败";
    document.getElementById("api-balance").className = "cost-val";
    document.getElementById("api-balance-sub").textContent = String(e).slice(0, 28);
    if (!silent) showToast(`余额查询失败: ${e}`);
  }
}

document.getElementById("btn-refresh-balance").addEventListener("click", () => refreshBalance(false));

document.getElementById("btn-reset-cost").addEventListener("click", async () => {
  const ok = await dialog.confirm("确定清空累计 Token 与费用统计吗？", {
    title: "重置成本统计",
    kind: "warning",
  });
  if (!ok) return;
  showToast(await invoke("reset_usage_stats"));
  await refreshStatus();
});

document.getElementById("btn-clear-cache").addEventListener("click", async () => {
  const ok = await dialog.confirm("确定清空答案缓存吗？（作答历史会保留）", {
    title: "清空答案缓存",
    kind: "warning",
  });
  if (!ok) return;
  showToast(await invoke("clear_answer_cache"));
  await refreshStatus();
});

async function handleExport(mode, defaultName) {
  const savePath = await dialog.save({
    defaultPath: defaultName,
    filters: [{ name: "CSV 表格", extensions: ["csv"] }],
  });
  if (!savePath) return;
  try {
    showToast(await invoke("export_csv_file", { path: savePath, mode }));
  } catch (e) {
    showToast(`导出失败: ${e}`);
  }
}
document.getElementById("btn-export-history").addEventListener("click", () => handleExport("history", "作答历史.csv"));
document.getElementById("btn-export-errors").addEventListener("click", () => handleExport("errors", "失败记录.csv"));

// ── 新增/修正弹窗 ────────────────────────────────────────────────
function openFixModal(item) {
  document.getElementById("fix-modal-heading").textContent = item.heading;
  document.getElementById("fix-title-input").value = item.title || "";
  document.getElementById("fix-options-input").value = item.options || "";
  document.getElementById("fix-type-input").value =
    ["single", "multiple", "judgement", "completion"].includes(item.q_type) ? item.q_type : "single";
  document.getElementById("fix-answer-text").value = item.answer_text || "";
  document.getElementById("fix-analysis").value = item.analysis || "";
  document.getElementById("fix-modal").classList.remove("hidden");
}

document.getElementById("btn-fix-cancel").addEventListener("click", () => {
  document.getElementById("fix-modal").classList.add("hidden");
});

document.getElementById("btn-fix-save").addEventListener("click", async () => {
  const title = document.getElementById("fix-title-input").value.trim();
  const options = document.getElementById("fix-options-input").value.trim();
  const qType = document.getElementById("fix-type-input").value;
  const answerText = document.getElementById("fix-answer-text").value.trim();
  const analysis = document.getElementById("fix-analysis").value.trim();
  if (!title || !answerText) {
    showToast("请填写题目和正确答案文本");
    return;
  }
  await invoke("save_local_bank_fix", { title, options, qType, answerText, analysis });
  document.getElementById("fix-modal").classList.add("hidden");
  showToast("已修正并清理旧缓存");
  await refreshStatus();
});

// ── 配置中心 (.env) ──────────────────────────────────────────────
async function loadEnvConfig() {
  const cfg = await invoke("read_env_config");
  const form = document.getElementById("env-form");
  for (const el of form.elements) {
    if (el.name && cfg[el.name] !== undefined) {
      el.value = cfg[el.name];
    }
  }
}

async function saveEnvForm() {
  const form = document.getElementById("env-form");
  const entries = {};
  for (const el of form.elements) {
    if (el.name) entries[el.name] = el.value.trim();
  }
  await invoke("save_env_config", { entries });
}

document.getElementById("btn-save-env").addEventListener("click", async () => {
  await saveEnvForm();
  showToast("配置已保存，重启 AI 服务后生效");
});

document.getElementById("btn-save-restart").addEventListener("click", async () => {
  await saveEnvForm();
  showToast("配置已保存，正在重启服务...");
  await invoke("stop_services");
  showToast(await invoke("start_services"));
  await refreshStatus();
});

document.getElementById("btn-toggle-key").addEventListener("click", () => {
  const input = document.getElementById("input-api-key");
  const btn = document.getElementById("btn-toggle-key");
  if (input.type === "password") {
    input.type = "text";
    btn.textContent = "隐藏";
  } else {
    input.type = "password";
    btn.textContent = "显示";
  }
});

// ── OCS 接入与快捷工具 ───────────────────────────────────────────
async function loadOcsPreview() {
  try {
    const content = await invoke("get_ocs_config");
    document.getElementById("ocs-json-preview").textContent = content;
  } catch (e) {
    document.getElementById("ocs-json-preview").textContent = `加载失败: ${e}`;
  }
}

document.getElementById("btn-copy-ocs").addEventListener("click", async () => {
  const content = await invoke("get_ocs_config");
  await navigator.clipboard.writeText(content);
  showToast("已复制 OCS 题库配置到剪贴板");
});

document.getElementById("btn-open-folder").addEventListener("click", () => invoke("open_external", { target: "project" }));
document.getElementById("btn-open-tiku").addEventListener("click", () => invoke("open_external", { target: "tiku_admin" }));
document.getElementById("btn-open-recent").addEventListener("click", () => invoke("open_external", { target: "recent_api" }));

// ── 初始化与轮询 ─────────────────────────────────────────────────
refreshStatus();
refreshBalance(true);
loadLogs();
setInterval(refreshStatus, 4000);
setInterval(() => refreshBalance(true), 30000);
setInterval(() => {
  if (
    document.getElementById("chk-auto-log").checked &&
    document.getElementById("tab-dashboard").classList.contains("active")
  ) {
    loadLogs();
  }
}, 2500);
