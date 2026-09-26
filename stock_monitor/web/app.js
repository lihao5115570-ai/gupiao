const fmt = (value, digits = 2) => value === null || value === undefined || value === "" ? "--" : (Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : "--");
const signed = (value) => value === null || value === undefined || value === "" ? "--" : `${Number(value) >= 0 ? "+" : ""}${fmt(value)}%`;
const safe = (value) => String(value ?? "").replace(/[&<>'"]/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"})[char]);

function positionTemplate(stock, index) {
  const reasons = (stock.reasons?.length ? stock.reasons : ["等待最新分析"]).slice(0, 4);
  const returnClass = Number(stock.return_pct) >= 0 ? "positive" : "negative";
  const advice = stock.position_advice ? `<div class="reasons advice"><h3>仓位观察建议</h3><p><strong>${safe(stock.position_advice)}</strong> ${safe(stock.position_advice_detail || "")}</p></div>` : "";
  return `<article class="position risk-${stock.risk_level} ${index === 0 ? "expanded" : ""}">
    <button class="position-head" type="button" aria-expanded="${index === 0}">
      <span class="risk-rank">${index + 1}</span>
      <span class="stock-name">${safe(stock.name)} <span class="stock-code">${safe(stock.code)}</span></span>
      <span class="risk-value">${stock.risk_level}/5</span>
    </button>
    <div class="position-body">
      <div class="metrics">
        <div class="metric"><span>现价</span><strong>${fmt(stock.price)}</strong></div>
        <div class="metric"><span>成本</span><strong>${fmt(stock.buy_price)}</strong></div>
        <div class="metric"><span>收益</span><strong class="${returnClass}">${signed(stock.return_pct)}</strong></div>
        <div class="metric"><span>回撤</span><strong>${fmt(stock.drawdown_pct)}%</strong></div>
      </div>
      <div class="reasons"><h3>风险原因</h3><ol>${reasons.map(reason => `<li>${safe(reason)}</li>`).join("")}</ol></div>
      ${advice}
      <div class="states"><h3>技术指标状态</h3>
        ${[["BOLL",stock.boll_state],["MACD",stock.macd_state],["KDJ",stock.kdj_state],["VOL",stock.volume_state]].map(([label,value]) => `<div class="state-row"><strong>${label}</strong><span>${safe(value || "等待分析")}</span><em>${safe(value || "--")}</em></div>`).join("")}
      </div>
      <div class="levels"><h3>关键位置</h3><div class="level-grid"><span>短线支撑 <strong>${fmt(stock.support)}</strong></span><span>周线支撑 <strong>${fmt(stock.major_support)}</strong></span><span>核心支撑区 <strong>${safe(stock.support_zone || "--")}</strong></span><span>日线博弈区 <strong>${safe(stock.play_zone || "--")}</strong></span><span>第一压力区 <strong>${safe(stock.pressure_zone || "--")}</strong></span><span>第二压力区 <strong>${safe(stock.second_pressure_zone || "--")}</strong></span><span>强压力区 <strong>${safe(stock.strong_pressure_zone || "--")}</strong></span></div><p class="zone-method">${safe(stock.zone_method || "")}</p></div>
    </div>
  </article>`;
}

async function loadData() {
  const button = document.querySelector("#refresh");
  button.disabled = true;
  try {
    const response = await fetch("/api/status", {cache: "no-store"});
    if (response.status === 401) { location.href = "/"; return; }
    if (!response.ok) throw new Error("request failed");
    const data = await response.json();
    document.querySelector("#holding-count").textContent = `持仓总数：${data.positions.length}`;
    document.querySelector("#updated-at").textContent = `更新时间 ${data.updated_at}`;
    document.querySelector("#positions").innerHTML = data.positions.length ? data.positions.map(positionTemplate).join("") : '<p class="empty">暂无持仓</p>';
    document.querySelectorAll(".position-head").forEach(head => head.addEventListener("click", () => {
      const item = head.closest(".position"); const expanded = item.classList.toggle("expanded"); head.setAttribute("aria-expanded", expanded);
    }));
    document.querySelector("#alerts").innerHTML = data.alerts.length ? data.alerts.map(alert => `<li><span class="alert-dot"></span><time class="alert-time">${safe(alert.time)}</time><span><strong>${safe(alert.name)} ${safe(alert.code)}</strong>　${safe(alert.reason)}</span></li>`).join("") : '<li class="empty">暂无提醒</li>';
  } catch (_error) {
    document.querySelector("#positions").innerHTML = '<p class="empty">暂时无法读取数据，请稍后刷新</p>';
  } finally { button.disabled = false; }
}

document.querySelector("#refresh").addEventListener("click", loadData);
loadData();
setInterval(loadData, 60000);

