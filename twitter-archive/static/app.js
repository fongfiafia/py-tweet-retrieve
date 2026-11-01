/* X/Twitter Archive Inspector — 原生 JS，无框架依赖 */

const state = {
  tweetsData: null,      // output/tweets.json 内容
  statusResults: null,   // output/x_status_results.json 内容
  recoveryResults: null, // output/media_recovery_results.json 内容
  filterStatus: "all",
  filterMedia: false,
};

const STATUS_TEXT = {
  active: "Active",
  deleted: "Deleted",
  unavailable: "Unavailable",
  unknown: "Unknown",
};

const $ = (sel) => document.querySelector(sel);

function esc(str) {
  if (str === null || str === undefined) return "";
  return String(str)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function fmtTimestamp(ts) {
  if (!ts || typeof ts !== "string" || ts.length < 14) return ts || "-";
  return `${ts.slice(0, 4)}-${ts.slice(4, 6)}-${ts.slice(6, 8)} ` +
         `${ts.slice(8, 10)}:${ts.slice(10, 12)}:${ts.slice(12, 14)}`;
}

/** 把 tweets.json 与 x_status_results.json 合并为统一视图 */
function buildView() {
  const tweets = (state.tweetsData && state.tweetsData.tweets) || [];
  const statusMap = new Map();
  if (state.statusResults && Array.isArray(state.statusResults.tweets)) {
    for (const rec of state.statusResults.tweets) {
      statusMap.set(String(rec.tweet_id), rec);
    }
  }
  return tweets.map((t) => {
    const rec = statusMap.get(String(t.tweet_id)) || null;
    return {
      tweet_id: String(t.tweet_id),
      original_url: t.original_url,
      snapshot_count: (t.snapshots || []).length,
      first_timestamp: t.first_timestamp,
      last_timestamp: t.last_timestamp,
      status: rec ? rec.current_status : "unknown",
      status_source: rec ? rec.status_source : null,
      http_status: rec ? rec.http_status : null,
      checked_at: rec ? rec.checked_at : null,
      evidence: rec ? rec.evidence : null,
      error: rec ? rec.error : null,
      text: rec ? rec.text : null,
      author: rec ? rec.author : null,
      media: (rec && Array.isArray(rec.media)) ? rec.media : [],
    };
  });
}

function renderStats(rows) {
  const counts = { active: 0, deleted: 0, unavailable: 0, unknown: 0 };
  let withMedia = 0;
  let totalMedia = 0;
  for (const r of rows) {
    counts[r.status] = (counts[r.status] || 0) + 1;
    if (r.media.length > 0) withMedia += 1;
    totalMedia += r.media.length;
  }
  const items = [
    ["Total Tweets", rows.length, ""],
    ["Active", counts.active, "active"],
    ["Deleted", counts.deleted, "deleted"],
    ["Unavailable", counts.unavailable, "unavailable"],
    ["Unknown", counts.unknown, "unknown"],
    ["Tweets with Media", withMedia, ""],
    ["Total Media", totalMedia, ""],
  ];
  $("#stats").innerHTML = items.map(([label, num, cls]) =>
    `<div class="stat ${cls}"><div class="num">${num}</div><div class="label">${esc(label)}</div></div>`
  ).join("");
}

function renderMedia(media) {
  if (!media || media.length === 0) {
    return `<div class="no-media">No media</div>`;
  }
  const items = media.map((m, i) => {
    const tag = `<span class="media-tag">${esc(m.type)}</span>`;
    if (m.type === "photo" && m.url) {
      return `<div class="media-item">${tag}
        <a href="${esc(m.url)}" target="_blank" rel="noopener">
          <img src="${esc(m.url)}" loading="lazy" alt="photo ${i + 1}"
               onerror="this.outerHTML='<div class=\\'media-fail\\'>图片加载失败<br>${esc(shortUrl(m.url))}</div>'">
        </a></div>`;
    }
    if ((m.type === "video" || m.type === "animated_gif") && m.url) {
      const poster = m.preview_image_url ? ` poster="${esc(m.preview_image_url)}"` : "";
      return `<div class="media-item">${tag}
        <video controls preload="none"${poster} src="${esc(m.url)}"
          onerror="this.outerHTML='<div class=\\'media-fail\\'>视频加载失败<br>${esc(shortUrl(m.url))}</div>'"
        ></video></div>`;
    }
    return `<div class="media-item"><div class="media-fail">媒体地址缺失</div></div>`;
  });
  return `<div class="media-grid">${items.join("")}</div>`;
}

function shortUrl(url) {
  try {
    const u = new URL(url);
    return u.hostname + u.pathname.slice(0, 28);
  } catch { return url; }
}

/** 恢复结果按 tweet_id 索引 */
function recoveryMap() {
  const map = new Map();
  if (state.recoveryResults && Array.isArray(state.recoveryResults.tweets)) {
    for (const t of state.recoveryResults.tweets) {
      map.set(String(t.tweet_id), t);
    }
  }
  return map;
}

/** deleted 推文卡片的媒体恢复区域 */
function renderRecoveryArea(tweetId, map) {
  const rec = map.get(String(tweetId));
  if (!rec) return "";
  const SRC_TEXT = { original_cdn: "Original CDN", wayback: "Wayback", none: "-" };
  const recText = (rec.recovered_text || rec.recovered_author)
    ? `<div class="recovery-text">
        ${rec.recovered_author ? `<div class="author">${esc(rec.recovered_author)} <span class="meta">恢复自存档</span></div>` : ""}
        ${rec.recovered_text ? `<p class="text">${esc(rec.recovered_text)}</p>` : ""}
      </div>`
    : "";
  let mediaHtml;
  if (!rec.media || rec.media.length === 0) {
    mediaHtml = `<div class="recovery-note">未发现媒体 URL（已检查快照 ${rec.snapshots_checked}/${rec.snapshots_total}）</div>`;
  } else {
    mediaHtml = `<div class="media-grid">` + rec.media.map((m) => {
      const srcBadge = m.recovery_source === "original_cdn"
        ? `<span class="media-tag">Original CDN</span>`
        : m.recovery_source === "wayback"
          ? `<span class="media-tag">Wayback ${m.archive_timestamp ? esc(m.archive_timestamp.slice(0, 8)) : ""}</span>`
          : `<span class="media-tag">${esc(m.recovery_status)}</span>`;
      if (m.recovery_status === "recovered" && m.display_url) {
        const openLink = `<a class="open-link" href="${esc(m.display_url)}" target="_blank" rel="noopener">打开媒体</a>`;
        if (m.type === "photo") {
          return `<div class="media-item">${srcBadge}
            <a href="${esc(m.display_url)}" target="_blank" rel="noopener">
              <img src="${esc(m.display_url)}" loading="lazy" alt="recovered photo"
                   onerror="this.outerHTML='<div class=\\'media-fail\\'>图片加载失败</div>'">
            </a>${openLink}</div>`;
        }
        return `<div class="media-item">${srcBadge}
          <video controls preload="metadata" src="${esc(m.display_url)}"
            onerror="this.outerHTML='<div class=\\'media-fail\\'>视频加载失败</div>'"
          ></video>${openLink}</div>`;
      }
      return `<div class="media-item">${srcBadge}
        <div class="media-fail">未恢复 (${esc(m.recovery_status)})<br>${esc((m.evidence || m.error || "").slice(0, 90))}</div></div>`;
    }).join("") + `</div>`;
  }
  return `<div class="recovery">
    <div class="media-count">媒体恢复: 找到 ${rec.media_count} · 恢复 ${rec.recovered_media_count} · 已检查快照 ${rec.snapshots_checked}/${rec.snapshots_total}${rec.pic_twitter_links ? ` · pic.twitter.com 链接 ${rec.pic_twitter_links} 个` : ""}</div>
    ${recText}
    ${mediaHtml}
  </div>`;
}

function renderList(rows) {
  const list = $("#list");
  if (rows.length === 0) {
    list.innerHTML = `<div class="empty">没有推文数据。请先运行 main.py 生成 output/tweets.json，或点击 Debug Preview。</div>`;
    return;
  }
  const filtered = rows.filter((r) => {
    if (state.filterStatus !== "all" && r.status !== state.filterStatus) return false;
    if (state.filterMedia && r.media.length === 0) return false;
    return true;
  });
  if (filtered.length === 0) {
    list.innerHTML = `<div class="empty">当前筛选条件下没有推文。</div>`;
    return;
  }
  const recMap = recoveryMap();
  list.innerHTML = filtered.map((r) => {
    const xUrl = r.original_url || `https://x.com/i/web/status/${r.tweet_id}`;
    const author = r.author
      ? `<div class="author">${esc(r.author.name || "")} @${esc(r.author.screen_name || "")}</div>`
      : "";
    const evidence = r.evidence || r.error
      ? `<p class="evidence">evidence: ${esc(r.evidence || r.error)}</p>` : "";
    const checked = r.checked_at ? ` · 检测于 ${esc(r.checked_at)}` : "";
    const http = r.http_status ? ` · HTTP ${esc(r.http_status)}` : "";
    return `<article class="card ${r.status}">
      <div class="card-head">
        <a class="tweet-id" href="${esc(xUrl)}" target="_blank" rel="noopener">${esc(r.tweet_id)}</a>
        <span class="badge ${r.status}">${STATUS_TEXT[r.status] || esc(r.status)}</span>
        <span class="meta">快照 ${r.snapshot_count} · ${fmtTimestamp(r.first_timestamp)} ~ ${fmtTimestamp(r.last_timestamp)}${http}${checked}</span>
      </div>
      ${author}
      ${r.text ? `<p class="text">${esc(r.text)}</p>` : ""}
      ${evidence}
      ${renderMedia(r.media)}
      <div class="media-count">媒体数量: ${r.media.length}</div>
      ${r.status === "deleted" ? renderRecoveryArea(r.tweet_id, recMap) : ""}
    </article>`;
  }).join("");
}

function renderRecoveryStats(recovery) {
  const el = $("#recovery-stats");
  if (!recovery) {
    el.innerHTML = "";
    return;
  }
  const s = recovery.statistics || {};
  const items = [
    ["Deleted Tweets", recovery.total_deleted_tweets, "deleted"],
    ["Snapshots Checked", (recovery.tweets || []).reduce((a, t) => a + (t.snapshots_checked || 0), 0), ""],
    ["Media URLs Found", recovery.total_media_found, ""],
    ["Original CDN Accessible", s.original_cdn_accessible || 0, "active"],
    ["Wayback Media Recovered", s.wayback_recovered || 0, "active"],
    ["Not Archived", s.not_archived || 0, ""],
    ["Unknown", s.unknown || 0, "unknown"],
  ];
  el.innerHTML = items.map(([label, num, cls]) =>
    `<div class="stat ${cls}"><div class="num">${num}</div><div class="label">${esc(label)}</div></div>`
  ).join("");
}

function render() {
  const rows = buildView();
  renderStats(rows);
  renderRecoveryStats(state.recoveryResults);
  renderList(rows);
  $("#data-source").textContent =
    `data source: output/tweets.json` +
    (state.statusResults ? ` + output/x_status_results.json (${state.statusResults.generated_at})`
                         : ` (无状态检测结果)`) +
    (state.recoveryResults ? ` + output/media_recovery_results.json (${state.recoveryResults.generated_at})` : "");
}

async function loadData() {
  const resp = await fetch("/api/data");
  const data = await resp.json();
  state.tweetsData = data.tweets_data;
  state.statusResults = data.status_results;
  state.recoveryResults = data.recovery_results || null;
  render();
}

function setButtonsDisabled(disabled) {
  $("#btn-inspect").disabled = disabled;
  $("#btn-debug").disabled = disabled;
}

// ---- Start Inspection：只调用本地 API 检测 X，不请求 Wayback ----
async function startInspection() {
  setButtonsDisabled(true);
  try {
    const resp = await fetch("/api/inspect", { method: "POST" });
    if (resp.status === 409) {
      const data = await resp.json();
      alert(data.error || "检测正在进行中");
      return;
    }
    if (!resp.ok) {
      const data = await resp.json();
      alert(data.error || "启动检测失败");
      return;
    }
    pollProgress();
  } finally {
    setButtonsDisabled(false);
  }
}

function pollProgress() {
  const timer = setInterval(async () => {
    let p;
    try {
      const resp = await fetch("/api/inspect/progress");
      p = await resp.json();
    } catch { return; }
    const pct = p.total > 0 ? ` (${Math.round((p.done / p.total) * 100)}%)` : "";
    $("#progress").textContent = p.finished
      ? `检测结束: 成功 ${p.success} / 失败 ${p.failed}${p.error ? " · 错误: " + p.error : ""}`
      : `检测中 ${p.done}/${p.total}${pct} · 当前: ${p.current_tweet_id || "-"} · 成功 ${p.success} 失败 ${p.failed}`;
    if (p.finished) {
      clearInterval(timer);
      await loadData();
    }
  }, 1000);
}

// ---- Recover Deleted Media：只针对 deleted 推文恢复历史媒体，不执行 Syndication ----
async function startRecovery() {
  setButtonsDisabled(true);
  try {
    const resp = await fetch("/api/recover", { method: "POST" });
    if (resp.status === 409) {
      const data = await resp.json();
      alert(data.error || "恢复任务正在进行中");
      return;
    }
    if (!resp.ok) {
      const data = await resp.json();
      alert(data.error || "启动恢复失败");
      return;
    }
    pollRecovery();
  } finally {
    setButtonsDisabled(false);
  }
}

function pollRecovery() {
  const timer = setInterval(async () => {
    let p;
    try {
      const resp = await fetch("/api/recover/progress");
      p = await resp.json();
    } catch { return; }
    const pct = p.total > 0 ? ` (${Math.round((p.done / p.total) * 100)}%)` : "";
    $("#progress").textContent = p.finished
      ? `恢复结束: 成功 ${p.success} / 失败 ${p.failed}${p.error ? " · 错误: " + p.error : ""}`
      : `恢复中 ${p.done || 0}/${p.total}${pct} · 当前: ${p.current_tweet_id || "-"} · 成功 ${p.success} 失败 ${p.failed}`;
    if (p.finished) {
      clearInterval(timer);
      await loadData();
    }
  }, 1000);
}

// ---- 事件绑定 ----
$("#btn-inspect").addEventListener("click", startInspection);
$("#btn-recover").addEventListener("click", startRecovery);
$("#btn-debug").addEventListener("click", loadData); // Debug Preview：只读本地 JSON，零外部请求
$("#btn-reload").addEventListener("click", loadData);
$("#filter-status").addEventListener("change", (e) => {
  state.filterStatus = e.target.value;
  render();
});
$("#filter-media").addEventListener("change", (e) => {
  state.filterMedia = e.target.checked;
  render();
});

loadData();
