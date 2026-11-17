"use strict";

const state = {
  q: "",
  source: "all",
  exchange: "all",
  type: "all",
  registered: "all",
  sort: "turnover_desc",
  rankFocus: "global",
  page: 1,
  perPage: 50,
  pages: 1,
  items: [],
  currentRankTotal: 0,
};

const $ = (selector) => document.querySelector(selector);
const number = new Intl.NumberFormat("zh-CN");
const compact = new Intl.NumberFormat("en-US", {
  notation: "compact",
  maximumFractionDigits: 2,
});
const decimal = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });
let searchTimer;
const catalog = window.STOCK_LISTINGS_DATA || { items: [], source_created_at: "" };

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, char => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
}

function statusMarkup(item) {
  if (!item.registered) return '<span class="status">未注册</span>';
  if (item.registration?.is_active) return '<span class="status registered">已注册 · 启用</span>';
  return '<span class="status inactive">已注册 · 停用</span>';
}

function money(value, currency = "USD", compactValue = false) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
  const prefix = currency === "USD" ? "$" : `${currency || ""} `;
  return `${prefix}${compactValue ? compact.format(Number(value)) : decimal.format(Number(value))}`;
}

function percent(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
  const numeric = Number(value);
  return `${numeric > 0 ? "+" : ""}${decimal.format(numeric)}%`;
}

function changeClass(value) {
  if (Number(value) > 0) return "change-positive";
  if (Number(value) < 0) return "change-negative";
  return "";
}

function renderRows(items) {
  $("#listingsBody").innerHTML = items.map((item, index) => `
    <tr data-index="${index}" tabindex="0">
      <td>
        <span class="rank-line ${state.rankFocus === "global" ? "rank-primary" : "rank-secondary"}">${item.market_data?.global_rank ? `#${number.format(item.market_data.global_rank)}` : "—"}</span>
        <span class="rank-line rank-filtered ${state.rankFocus === "filtered" ? "rank-primary" : "rank-secondary"}">${item.current_rank ? `筛#${number.format(item.current_rank)}` : ""}</span>
      </td>
      <td><div class="symbol-cell">${item.registered ? '<span class="registered-mark"></span>' : ""}<strong>${esc(item.symbol)}</strong></div></td>
      <td class="company-name" title="${esc(item.name)}">${esc(item.name)}</td>
      <td><span class="subtle">${esc(item.market)}</span></td>
      <td><span class="pill">${esc(item.market_data?.category || item.security_type)}</span></td>
      <td>
        <span class="market-primary">${money(item.market_data?.price, item.market_data?.currency)}</span>
        <span class="market-secondary ${changeClass(item.market_data?.change_pct)}">${percent(item.market_data?.change_pct)}</span>
      </td>
      <td title="${item.market_data?.turnover === null || item.market_data?.turnover === undefined ? "暂无 TradingView 成交额" : esc(money(item.market_data.turnover, item.market_data.turnover_currency))}">
        <span class="turnover ${item.market_data?.turnover === null || item.market_data?.turnover === undefined ? "turnover-missing" : ""}">${money(item.market_data?.turnover, item.market_data?.turnover_currency, true)}</span>
        <span class="market-secondary">${item.market_data?.relative_volume === null || item.market_data?.relative_volume === undefined ? "" : `相对量 ${decimal.format(item.market_data.relative_volume)}×`}</span>
      </td>
      <td>${statusMarkup(item)}</td>
      <td><span class="row-arrow">→</span></td>
    </tr>
  `).join("");

  $("#listingsBody").querySelectorAll("tr").forEach(row => {
    const open = () => openDetail(items[Number(row.dataset.index)]);
    row.addEventListener("click", open);
    row.addEventListener("keydown", event => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        open();
      }
    });
  });
}

function renderPagination(pagination) {
  state.page = pagination.page;
  state.pages = pagination.pages;
  const start = pagination.total ? (pagination.page - 1) * pagination.per_page + 1 : 0;
  const end = Math.min(pagination.page * pagination.per_page, pagination.total);
  $("#rangeStart").textContent = number.format(start);
  $("#rangeEnd").textContent = number.format(end);
  $("#resultTotal").textContent = number.format(pagination.total);
  $("#prevPage").disabled = pagination.page <= 1;
  $("#nextPage").disabled = pagination.page >= pagination.pages;

  const candidates = new Set([1, pagination.pages, pagination.page - 1, pagination.page, pagination.page + 1]);
  const pages = [...candidates].filter(page => page >= 1 && page <= pagination.pages).sort((a, b) => a - b);
  let previous = 0;
  $("#pageButtons").innerHTML = pages.map(page => {
    const gap = page - previous > 1 ? '<button disabled>…</button>' : "";
    previous = page;
    return `${gap}<button data-page="${page}" class="${page === pagination.page ? "active" : ""}">${page}</button>`;
  }).join("");
  $("#pageButtons").querySelectorAll("[data-page]").forEach(button => {
    button.addEventListener("click", () => changePage(Number(button.dataset.page)));
  });
}

function fillSelect(element, values, label) {
  const current = element.value;
  element.innerHTML = `<option value="all">${label}</option>` +
    values.map(value => `<option value="${esc(value)}">${esc(value)}</option>`).join("");
  element.value = values.includes(current) ? current : "all";
}

function updateSummary(data) {
  $("#metricListed").textContent = number.format(data.summary.listed);
  $("#metricEligible").textContent = number.format(data.summary.eligible);
  $("#metricExcluded").textContent = number.format(data.summary.excluded);
  $("#metricRegistered").textContent = number.format(data.summary.registered);
  $("#metricActive").textContent = number.format(data.summary.active);
  $("#sourceTime").textContent = data.liquidity_as_of || "—";
}

function compareNullable(left, right, direction) {
  const leftMissing = left === null || left === undefined || !Number.isFinite(Number(left));
  const rightMissing = right === null || right === undefined || !Number.isFinite(Number(right));
  if (leftMissing && rightMissing) return 0;
  if (leftMissing) return 1;
  if (rightMissing) return -1;
  return (Number(left) - Number(right)) * direction;
}

function tickerSearchRank(item, query) {
  if (!query) return 0;
  const symbol = String(item.symbol || "").toUpperCase();
  const altSymbol = String(item.alt_symbol || "").toUpperCase();
  const normalizedQuery = query.toUpperCase();
  if (symbol === normalizedQuery || altSymbol === normalizedQuery) return 0;
  if (symbol.startsWith(normalizedQuery) || altSymbol.startsWith(normalizedQuery)) return 1;
  if (symbol.includes(normalizedQuery) || altSymbol.includes(normalizedQuery)) return 2;
  return 3;
}

function sortItems(items) {
  return items.sort((a, b) => {
    if (state.q) {
      const relevance = tickerSearchRank(a, state.q) - tickerSearchRank(b, state.q);
      if (relevance) return relevance;
    }
    let result = 0;
    if (state.sort === "turnover_desc") {
      result = compareNullable(a.market_data?.turnover, b.market_data?.turnover, -1);
    } else if (state.sort === "turnover_asc") {
      result = compareNullable(a.market_data?.turnover, b.market_data?.turnover, 1);
    } else if (state.sort === "change_desc") {
      result = compareNullable(a.market_data?.change_pct, b.market_data?.change_pct, -1);
    } else {
      result = a.symbol.localeCompare(b.symbol);
    }
    return result || a.symbol.localeCompare(b.symbol);
  });
}

function loadListings() {
  $("#loadingState").hidden = false;
  $("#errorState").hidden = true;
  $("#listingsTable").hidden = true;
  $("#emptyState").hidden = true;

  try {
    if (!Array.isArray(catalog.items) || catalog.items.length === 0) {
      throw new Error("没有找到 data.js，请先运行 build_static.py 生成页面数据。");
    }
    const query = state.q.toUpperCase();
    const filtered = catalog.items.filter(item => {
      if (query && tickerSearchRank(item, query) >= 3) return false;
      if (state.source !== "all" && item.source !== state.source) return false;
      if (state.exchange !== "all" && item.exchange !== state.exchange) return false;
      if (state.type !== "all" && item.security_type !== state.type) return false;
      if (state.registered === "yes" && !item.registered) return false;
      if (state.registered === "no" && item.registered) return false;
      if (state.registered === "active" && !(item.registered && item.registration?.is_active)) return false;
      return true;
    });

    const rankedForCurrentFilter = filtered
      .filter(item => item.market_data?.turnover !== null && item.market_data?.turnover !== undefined)
      .sort((a, b) => (
        Number(b.market_data.turnover) - Number(a.market_data.turnover)
        || a.symbol.localeCompare(b.symbol)
      ));
    state.currentRankTotal = rankedForCurrentFilter.length;
    const currentRanks = new Map(
      rankedForCurrentFilter.map((item, index) => [item.symbol, index + 1])
    );
    sortItems(filtered);

    const total = filtered.length;
    const pages = Math.max(1, Math.ceil(total / state.perPage));
    state.page = Math.min(state.page, pages);
    const start = (state.page - 1) * state.perPage;
    const pageItems = filtered
      .slice(start, start + state.perPage)
      .map(item => ({ ...item, current_rank: currentRanks.get(item.symbol) || null }));
    const data = {
      items: pageItems,
      source_created_at: catalog.source_created_at,
      liquidity_as_of: catalog.liquidity_as_of,
      summary: {
        listed: catalog.universe_summary?.listed_total ?? catalog.items.length,
        eligible: catalog.universe_summary?.eligible_total ?? catalog.items.length,
        excluded: catalog.universe_summary?.excluded_total ?? 0,
        registered: catalog.universe_summary?.eligible_registered ?? catalog.items.filter(item => item.registered).length,
        active: catalog.universe_summary?.eligible_active ?? catalog.items.filter(item => item.registered && item.registration?.is_active).length,
      },
      facets: {
        exchanges: [...new Set(catalog.items.map(item => item.exchange))].sort(),
        types: [...new Set(catalog.items.map(item => item.security_type))].sort(),
      },
      pagination: { page: state.page, pages, per_page: state.perPage, total },
    };
    state.items = pageItems;
    updateSummary(data);
    fillSelect($("#exchangeFilter"), data.facets.exchanges, "全部交易所");
    fillSelect($("#typeFilter"), data.facets.types, "全部类型");
    renderRows(pageItems);
    renderPagination(data.pagination);
    $("#listingsTable").hidden = pageItems.length === 0;
    $("#emptyState").hidden = pageItems.length !== 0;
  } catch (error) {
    $("#errorText").textContent = error.message;
    $("#errorState").hidden = false;
  } finally {
    $("#loadingState").hidden = true;
  }
}

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.add("show");
  window.setTimeout(() => toast.classList.remove("show"), 1700);
}

function openDetail(item) {
  const registration = item.registration || {};
  const marketData = item.market_data || {};
  const registered = item.registered;
  const active = Boolean(registration.is_active);
  $("#dialogContent").innerHTML = `
    <div class="dialog-head">
      <small>${esc(item.market)} · ${esc(item.security_type)}</small>
      <h3>${esc(item.symbol)}</h3>
      <p>${esc(item.name)}</p>
    </div>
    <div class="dialog-body">
      <span class="dialog-status">${registered ? `● 已注册${active ? " · 当前启用" : " · 当前停用"}` : "○ 尚未注册"}</span>
      <div class="dialog-grid">
        <div><span>当前价格</span><strong>${money(marketData.price, marketData.currency)}</strong></div>
        <div><span>一日涨跌</span><strong class="${Number(marketData.change_pct) > 0 ? "positive" : Number(marketData.change_pct) < 0 ? "negative" : ""}">${percent(marketData.change_pct)}</strong></div>
        <div><span>一个月成交额</span><strong>${money(marketData.turnover, marketData.turnover_currency, true)}</strong></div>
        <div><span>可交易名录排名</span><strong>${marketData.global_rank ? `#${number.format(marketData.global_rank)} / ${number.format(catalog.universe_summary?.eligible_total || catalog.items.length)}` : "—"}</strong></div>
        <div><span>当前筛选排名</span><strong>${item.current_rank ? `#${number.format(item.current_rank)} / ${number.format(state.currentRankTotal)}` : "—"}</strong></div>
        <div><span>相对成交量</span><strong>${marketData.relative_volume === null || marketData.relative_volume === undefined ? "—" : `${decimal.format(marketData.relative_volume)}×`}</strong></div>
        <div><span>${marketData.category === "ETF" ? "资产管理规模" : "市值"}</span><strong>${money(marketData.market_value, marketData.market_value_currency, true)}</strong></div>
        <div><span>上市来源</span><strong>${esc(item.source)}</strong></div>
        <div><span>交易市场</span><strong>${esc(item.market)}</strong></div>
        <div><span>资产类别</span><strong>${esc(registration.asset_class || (item.is_etf ? "ETF" : "—"))}</strong></div>
        <div><span>数据库类型</span><strong>${esc(registration.instrument_type || "—")}</strong></div>
        <div><span>GICS 行业</span><strong>${esc(registration.sector || "—")}</strong></div>
        <div><span>标准交易单位</span><strong>${number.format(item.round_lot || 0)}</strong></div>
      </div>
      <div class="dialog-actions">
        <button type="button" id="copySymbol">复制 ${esc(item.symbol)}</button>
      </div>
      ${registered ? '<p class="dialog-note">该证券已存在于 quant_data.us_info；页面只读，不会修改注册信息。</p>' : ""}
      <p class="dialog-note">行情来自 TradingView ${esc(catalog.liquidity_as_of || "")} 月度快照；排名已排除无月成交额证券和 Cboe BZX 杠杆 ETF。</p>
    </div>
  `;
  $("#detailDialog").showModal();
  $("#copySymbol").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(item.symbol);
    } catch {
      const helper = document.createElement("textarea");
      helper.value = item.symbol;
      document.body.appendChild(helper);
      helper.select();
      document.execCommand("copy");
      helper.remove();
    }
    showToast(`已复制 ${item.symbol}`);
  });
}

function changePage(page) {
  if (page < 1 || page > state.pages || page === state.page) return;
  state.page = page;
  loadListings();
  $(".directory").scrollIntoView({ behavior: "smooth", block: "start" });
}

function bindSegments(selector, key) {
  $(selector).querySelectorAll("button").forEach(button => {
    button.addEventListener("click", () => {
      $(selector).querySelector(".active")?.classList.remove("active");
      button.classList.add("active");
      state[key] = button.dataset.value;
      state.page = 1;
      loadListings();
    });
  });
}

$("#searchInput").addEventListener("input", event => {
  clearTimeout(searchTimer);
  searchTimer = window.setTimeout(() => {
    state.q = event.target.value.trim();
    state.page = 1;
    loadListings();
  }, 240);
});
$("#exchangeFilter").addEventListener("change", event => {
  state.exchange = event.target.value; state.page = 1; loadListings();
});
$("#typeFilter").addEventListener("change", event => {
  state.type = event.target.value; state.page = 1; loadListings();
});
$("#sortSelect").addEventListener("change", event => {
  state.sort = event.target.value; state.page = 1; loadListings();
});
$("#rankFocusToggle").addEventListener("click", () => {
  state.rankFocus = state.rankFocus === "global" ? "filtered" : "global";
  $("#rankFocusLabel").textContent = state.rankFocus === "global" ? "基准大" : "筛选大";
  loadListings();
});
$("#prevPage").addEventListener("click", () => changePage(state.page - 1));
$("#nextPage").addEventListener("click", () => changePage(state.page + 1));
$("#retryButton").addEventListener("click", loadListings);
$("#dialogClose").addEventListener("click", () => $("#detailDialog").close());
$("#detailDialog").addEventListener("click", event => {
  if (event.target === $("#detailDialog")) $("#detailDialog").close();
});
document.addEventListener("keydown", event => {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
    event.preventDefault(); $("#searchInput").focus();
  }
});

bindSegments("#sourceSegments", "source");
bindSegments("#registerSegments", "registered");
loadListings();
