/**
 * 小黑盒 AI 自动回复机器人 - 前端主逻辑
 */

// ==============================
// 全局常量
// ==============================

const QR_POLL_INTERVAL_MS = 1500;   // 二维码状态轮询间隔
const QR_POLL_MAX = 200;            // 二维码状态轮询最大次数
const SSE_RECONNECT_MS = 3000;      // SSE 断线重连初始退避
const SSE_RECONNECT_MAX_MS = 30000; // SSE 断线重连退避上限
const DASHBOARD_REFRESH_MS = 2000;  // 仪表盘刷新间隔
const STATUS_REFRESH_MS = 5000;     // 机器人状态刷新间隔
const LOG_MAX_ENTRIES = 500;        // 日志区最大保留条数
const MODAL_AUTOCLOSE_MS = 2000;    // 提示弹窗自动关闭时间
const API_TIMEOUT_MS = 30000;       // API 请求超时时间
const LLM_TEST_TIMEOUT_MS = 120000; // LLM/搜索连通性测试超时（需真实调用外部接口）

// ==============================
// 全局状态
// ==============================
const STATE = {
  loggedIn: false,
  botRunning: false,
  currentConfig: {},
  providers: {},
  eventSource: null,
  countdownTimer: null,
  waitTotal: 0,
  waitStartTime: 0,
  qrPollId: 0,      // 二维码轮询代数，新轮询作废旧轮询
  altAccounts: [],  // 副号列表缓存（替身下拉等使用）
};

// SSE 重连退避（连接成功后重置）
let _sseReconnectDelay = SSE_RECONNECT_MS;

// showModal autoClose 定时器句柄
let _autoCloseTimer = null;

// ==============================
// 工具函数
// ==============================

function $(id) { return document.getElementById(id); }

async function api(method, url, data = null, timeoutMs = API_TIMEOUT_MS) {
  const opts = {
    method,
    headers: {
      'Content-Type': 'application/json',
      'X-Api-Token': window.__API_TOKEN__ || '',
    },
    signal: AbortSignal.timeout(timeoutMs),
  };
  if (data) opts.body = JSON.stringify(data);
  const resp = await fetch(url, opts);
  if (!resp.ok) {
    const text = await resp.text().catch(() => '');
    throw new Error(`HTTP ${resp.status}${text ? ': ' + text.substring(0, 200) : ''}`);
  }
  try {
    return await resp.json();
  } catch (e) {
    throw new Error('响应格式错误');
  }
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

// 头像 URL 仅允许 http(s)，不合格则当作无头像走 onerror 兜底分支
function isSafeAvatarUrl(url) {
  return typeof url === 'string' && /^https?:\/\//i.test(url);
}

// 在 select 中按 value 查找 option（避免 option[value="..."] 选择器注入）
function findOption(select, name) {
  for (const opt of select.options) {
    if (opt.value === name) return opt;
  }
  return null;
}

// ==============================
// 模态窗口
// ==============================

function showModal(title, content, isDanger = false, autoCloseMs = 0) {
  const container = $('modalContainer');
  const dangerClass = isDanger ? ' modal-danger' : '';
  container.innerHTML = `
    <div class="modal-overlay" onclick="closeModal(event)">
      <div class="modal${dangerClass}" onclick="event.stopPropagation()">
        <div class="modal-header">
          <h3>${escapeHtml(title)}</h3>
          <button class="modal-close" onclick="closeModal()">&times;</button>
        </div>
        <div class="modal-body">${content}</div>
      </div>
    </div>
  `;
  clearTimeout(_autoCloseTimer);
  if (autoCloseMs > 0) {
    _autoCloseTimer = setTimeout(closeModal, autoCloseMs);
  }
}

// 统一渲染辅助：纯文本信息（先转义再把换行转为 <br>）
function showInfoModal(title, text) {
  showModal(title, `<div class="alert alert-info">${escapeHtml(String(text || '')).replace(/\n/g, '<br>')}</div>`);
}

// 统一渲染辅助：错误信息（一律转义，防 XSS）
function showErrorModal(title, err) {
  showModal(title, `<div class="alert alert-danger">${escapeHtml(String(err || '未知错误'))}</div>`);
}

function showConfirmModal(title, content, onConfirm, isDanger = false) {
  const container = $('modalContainer');
  const dangerClass = isDanger ? ' modal-danger' : '';
  container.innerHTML = `
    <div class="modal-overlay">
      <div class="modal${dangerClass}">
        <div class="modal-header">
          <h3>${escapeHtml(title)}</h3>
          <button class="modal-close" onclick="closeModal()">&times;</button>
        </div>
        <div class="modal-body">${content}</div>
        <div class="modal-footer">
          <button class="btn btn-outline" onclick="closeModal()">取消</button>
          <button class="btn ${isDanger ? 'btn-danger' : 'btn-primary'} confirm-btn">确认</button>
        </div>
      </div>
    </div>
  `;
  // 容器内查询，避免固定 id 在模态嵌套时绑错按钮
  container.querySelector('.confirm-btn').addEventListener('click', () => {
    closeModal();
    if (onConfirm) onConfirm();
  });
}

function closeModal(e) {
  if (e && e.target !== e.currentTarget) return;
  clearTimeout(_autoCloseTimer);
  $('modalContainer').innerHTML = '';
}

// ==============================
// 二维码渲染（本地生成）
// ==============================

// 使用本地 vendor 库（qrcode-generator, MIT）生成二维码，
// 避免把 qr_url 发给第三方二维码服务导致登录信息泄露
function renderQRCode(container, text) {
  container.innerHTML = '';
  if (typeof qrcode === 'undefined' || !text) {
    container.innerHTML = '<p class="muted">二维码生成失败，请刷新重试</p>';
    return;
  }
  const qr = qrcode(0, 'M');
  qr.addData(text);
  qr.make();
  container.innerHTML = qr.createImgTag(4, 0);
  const img = container.querySelector('img');
  if (img) {
    img.width = 200;
    img.height = 200;
  }
}

// ==============================
// 登录相关
// ==============================

async function showLoginModal() {
  try {
    const resp = await api('GET', '/api/login/qrcode');
    if (!resp.ok) {
      showErrorModal('获取二维码失败', resp.error);
      return;
    }

    const data = resp.data;
    const qrIdShort = (data.qr_id || '(无)').substring(0, 12);
    showModal('扫码登录', `
      <div class="qr-container">
        <div id="qrBox"></div>
        <div class="qr-status" id="qrStatus">请使用小黑盒 APP 扫描二维码</div>
        <p class="small mute" style="margin-top:8px">qr_id: ${escapeHtml(qrIdShort)}...</p>
      </div>
    `);
    renderQRCode($('qrBox'), data.qr_url);

    pollQRState(data.qr_id);
  } catch (e) {
    showErrorModal('请求失败', e.message);
  }
}

async function pollQRState(qrId) {
  const myId = ++STATE.qrPollId;
  const statusEl = $('qrStatus');
  if (!statusEl) return;

  let retries = 0;
  while (retries < QR_POLL_MAX) {
    // 已有更新的轮询在进行：旧轮询作废
    if (myId !== STATE.qrPollId) return;
    // 弹窗已关闭：停止轮询
    if (statusEl.isConnected === false) return;
    try {
      const resp = await api('GET', `/api/login/qrstate/${qrId}`);
      if (!resp.ok) {
        statusEl.textContent = '查询状态失败';
        return;
      }

      if (resp.state === 'ok') {
        closeModal();
        await checkLoginStatus();
        const avatarHtml = isSafeAvatarUrl(resp.avatar)
          ? `<img src="${escapeHtml(resp.avatar)}" style="width:60px;height:60px;border-radius:50%;border:2px solid var(--accent)">`
          : '';
        showModal('登录成功', `
          <div style="text-align:center;padding:10px;">
            <p style="font-size:16px;margin-bottom:8px;">欢迎，<strong>${escapeHtml(resp.nickname || '')}</strong></p>
            ${avatarHtml}
          </div>
        `);
        return;
      } else if (resp.state === 'ready') {
        statusEl.textContent = '扫码成功，请在 APP 中确认登录...';
      } else if (resp.state === 'wait') {
        statusEl.textContent = '等待扫码...';
      } else {
        statusEl.textContent = `状态: ${resp.state}`;
        return;
      }
    } catch (e) {
      statusEl.textContent = '网络错误，稍后重试';
    }

    await sleep(QR_POLL_INTERVAL_MS);
    retries++;
  }

  if (myId !== STATE.qrPollId || statusEl.isConnected === false) return;
  statusEl.textContent = '二维码已超时，请重新获取';
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function checkLoginStatus() {
  try {
    const resp = await api('GET', '/api/login/status');
    STATE.loggedIn = resp.is_logged_in;

    const missionUserName = $('missionUserName');
    const missionUserMeta = $('missionUserMeta');
    const missionUserAvatar = $('missionUserAvatar');
    const btnCheck = $('btnCheckStatus');
    const btnLogout = $('btnLogout');
    const btnReset = $('btnResetConfig');

    if (resp.is_logged_in) {
      const safeAvatar = isSafeAvatarUrl(resp.avatar);
      const avatarHtml = safeAvatar
        ? `<img src="${escapeHtml(resp.avatar)}" class="user-chip-avatar" onerror="this.onerror=null;this.outerHTML='<span class=\\'user-chip-dot online\\'></span>'">`
        : '<span class="user-chip-dot online"></span>';
      $('userChip').innerHTML = `
        ${avatarHtml}
        <span class="user-chip-text">${escapeHtml(resp.nickname || '已登录')}</span>
      `;

      missionUserName.textContent = resp.nickname || '已登录';
      missionUserMeta.textContent = `等级 ${resp.level || 0}`;
      missionUserAvatar.innerHTML = safeAvatar
        ? `<img src="${escapeHtml(resp.avatar)}" onerror="this.style.display='none';this.parentElement.classList.remove('has-avatar')">`
        : '';
      missionUserAvatar.classList.toggle('has-avatar', safeAvatar);

      if (btnCheck) btnCheck.style.display = '';
      if (btnLogout) btnLogout.style.display = '';
      if (btnReset) btnReset.style.display = '';

    } else {
      $('userChip').innerHTML = `
        <span class="user-chip-dot offline"></span>
        <span class="user-chip-text">未登录</span>
      `;

      missionUserName.textContent = '未登录';
      missionUserMeta.textContent = '点击登录小黑盒账号';
      missionUserAvatar.innerHTML = '';
      missionUserAvatar.classList.remove('has-avatar');

      if (btnCheck) btnCheck.style.display = 'none';
      if (btnLogout) btnLogout.style.display = 'none';
      if (btnReset) btnReset.style.display = '';
    }
  } catch (e) {
    console.error('检查登录状态失败:', e);
    STATE.loggedIn = false;
    return false;
  }
  return true;
}

async function handleCheckLogin() {
  showModal('检查登录状态', '<p style="text-align:center;">正在检测...</p>');
  const ok = await checkLoginStatus();
  closeModal();
  if (!ok) {
    showErrorModal('检测失败', '网络异常或服务不可用，请稍后重试');
    return;
  }
  if (STATE.loggedIn) {
    showModal('登录状态有效', '<p>当前登录会话处于有效状态。</p>', false, MODAL_AUTOCLOSE_MS);
  } else {
    showModal('未登录或已失效', `
      <div class="alert alert-warning">
        登录状态已失效或尚未登录。<br>请点击「登录」重新登录。
      </div>
    `);
  }
}

async function handleLogout() {
  showConfirmModal('确认退出', '<p>确定要退出登录吗？退出后机器人将自动停止。</p>', async () => {
    try {
      await api('POST', '/api/login/logout');
      STATE.loggedIn = false;
      STATE.botRunning = false;
      updateBotUI();
      await checkLoginStatus();
    } catch (e) {
      showErrorModal('请求失败', e.message);
    }
  });
}

// ==============================
// API Key 按提供商记忆
// ==============================

function restoreVendorKey(configKey, vendor, inputId) {
  if (!vendor) return;
  const cfg = STATE.currentConfig[configKey] || {};
  const vendorKeys = cfg.vendor_keys || {};
  if (vendorKeys[vendor]) {
    $(inputId).value = vendorKeys[vendor];
  } else {
    $(inputId).value = '';
  }
}

// ==============================
// 配置加载
// ==============================

async function loadConfig() {
  try {
    const resp = await api('GET', '/api/config');
    if (resp.ok) {
      STATE.currentConfig = resp.data;
      STATE.providers = resp.providers || {};
      applyConfig();
      populateVendors();
    }
  } catch (e) {
    console.error('加载配置失败:', e);
  }
}

async function loadPrompt() {
  try {
    const resp = await api('GET', '/api/config/prompt');
    if (resp.ok) {
      $('promptContent').value = resp.content || '';
    }
  } catch (e) {
    console.error('加载提示词失败:', e);
  }
}

function onModeChange() {
  const mode = $('botMode').value;
  $('whitelistGroup').style.display = mode === 'white_list' ? '' : 'none';
  $('frequencyGroup').style.display = mode === 'frequency' ? '' : 'none';
}

function populateVendors() {
  const select = $('vendorSelect');
  Object.keys(STATE.providers).forEach(name => {
    if (!findOption(select, name)) {
      const opt = document.createElement('option');
      opt.value = name;
      opt.textContent = name;
      select.appendChild(opt);
    }
  });
}

function onVendorChange() {
  const vendor = $('vendorSelect').value;
  if (vendor && vendor !== '自定义' && STATE.providers[vendor]) {
    const info = STATE.providers[vendor];
    $('baseUrl').value = info.base_url || '';
    if (info.model) {
      $('modelInput').value = info.model;
    }
  }
  // 从 vendor_keys 恢复该提供商的 API Key
  restoreVendorKey('llm', vendor, 'apiKey');
}

function onSearchVendorChange() {
  const vendor = $('searchVendorSelect').value;
  if (vendor && vendor !== '自定义' && STATE.providers[vendor]) {
    const info = STATE.providers[vendor];
    $('searchBaseUrl').value = info.base_url || '';
    if (info.model) {
      $('searchModelInput').value = info.model;
    }
  }
  // 从 vendor_keys 恢复该提供商的 API Key
  restoreVendorKey('llm_search', vendor, 'searchApiKey');
}

function onWebSearchToggle() {
  const on = $('webSearch').checked;
  $('searchModelSection').style.display = on ? 'block' : 'none';
  saveWebSearchSilent();
}

function applyConfig() {
  const bot = STATE.currentConfig.bot || {};
  const llm = STATE.currentConfig.llm || {};
  const llmSearch = STATE.currentConfig.llm_search || {};

  $('botMode').value = bot.mode || 'white_list';
  $('whiteList').value = (bot.white_list || []).join('\n');
  $('frequencyLimit').value = bot.frequency || 3;
  $('initWaitTime').value = bot.init_wait_time || 10;
  $('maxWaitTime').value = bot.max_wait_time || 60;
  $('increment').value = bot.increment || 10;
  $('maxMessagesPerRound').value = bot.max_messages_per_round || 3;
  $('parallelMode').checked = bot.parallel === true;
  $('parallelCount').value = bot.parallel_count || 5;
  $('parallelSection').style.display = bot.parallel ? 'block' : 'none';
  $('multiAccount').checked = bot.multi_account === true;
  $('standbyMode').checked = bot.standby_mode === true;
  $('autoLike').checked = bot.auto_like === true;
  // 总是加载副号列表：替身区块在交叉回复关闭时也需要可见可选
  refreshAltAccounts().then(() => {
    refreshStandbySlots();
    $('standbySlot').value = bot.standby_slot || '';
  });

  $('apiKey').value = llm.api_key || '';
  $('baseUrl').value = llm.base_url || '';
  $('apiPath').value = llm.api_path || '/chat/completions';
  $('maxTokens').value = llm.max_tokens || 5000;
  $('webSearch').checked = llm.web_search === true;
  $('showReasoning').checked = llm.show_reasoning === true;
  $('modelInput').value = llm.model || '';
  $('vendorSelect').value = llm.vendor || '';

  // 搜索模型配置
  $('searchApiKey').value = llmSearch.api_key || '';
  $('searchBaseUrl').value = llmSearch.base_url || '';
  $('searchModelInput').value = llmSearch.model || '';
  $('searchMaxTokens').value = llmSearch.max_tokens || 5000;
  $('searchVendorSelect').value = llmSearch.vendor || '';
  $('searchModelSection').style.display = $('webSearch').checked ? 'block' : 'none';

  // 搜索 API 配置
  const baiduCfg = STATE.currentConfig.llm_baidu_search || {};
  $('searchProviderSelect').value = baiduCfg.provider || 'baidu';
  $('baiduApiKey').value = baiduCfg.baidu_api_key || '';
  $('tavilyApiKey').value = baiduCfg.tavily_api_key || '';
  $('baiduModelSelect').value = baiduCfg.model || 'deepseek-v3';
  // 判断模型配置
  const judgeCfg = STATE.currentConfig.llm_search_judge || {};
  $('searchJudgeEnabled').checked = judgeCfg.enabled === true;
  $('judgeVendorSelect').value = judgeCfg.vendor || '';
  $('judgeApiKey').value = judgeCfg.api_key || '';
  $('judgeBaseUrl').value = judgeCfg.base_url || '';
  $('judgeModelInput').value = judgeCfg.model || '';
  $('searchJudgeSection').style.display = $('searchJudgeEnabled').checked ? 'block' : 'none';
  populateJudgeVendors();

  onSearchProviderChange();

  onModeChange();
  onVendorChange();
  populateSearchVendors();
  // Steam LLM 模型配置
  const llmSteam = STATE.currentConfig.llm_steam || {};
  $('steamLLMApiKey').value = llmSteam.api_key || '';
  $('steamLLMBaseUrl').value = llmSteam.base_url || '';
  $('steamLLMModel').value = llmSteam.model || '';
  $('steamLLMMaxTokens').value = llmSteam.max_tokens || 5000;
  $('steamVendorSelect').value = llmSteam.vendor || '';
  populateSteamVendors();
  // Steam 配置
  const steamCfg = STATE.currentConfig.steam || {};
  $('steamEnabled').checked = steamCfg.enabled === true;
  $('steamApiKey').value = steamCfg.steam_api_key || '';
  $('steamTopGames').value = steamCfg.top_games_count || 20;
  $('steamAutoScrape').checked = steamCfg.auto_scrape !== false;
  $('steamSection').style.display = $('steamEnabled').checked ? 'block' : 'none';
  if ($('steamEnabled').checked) {
    refreshSteamScrapeStatus();
  }

  loadPrompt();
  loadSteamPrompt();
  loadSteamRecPrompt();
}

// ==============================
// Steam 库存评价
// ==============================

function onSteamToggle() {
  $('steamSection').style.display = $('steamEnabled').checked ? 'block' : 'none';
}

async function saveSteamConfig() {
  try {
    await api('POST', '/api/config/steam', {
      enabled: $('steamEnabled').checked,
      steam_api_key: $('steamApiKey').value,
      top_games_count: parseInt($('steamTopGames').value, 10) || 20,
      auto_scrape: $('steamAutoScrape').checked,
    });
    await loadConfig();
    showModal('已保存', '<p>Steam 库存评价配置已更新</p>', false, MODAL_AUTOCLOSE_MS);
  } catch (e) {
    showErrorModal('请求失败', e.message);
  }
}

function onSteamAutoScrapeToggle() {
  const on = $('steamAutoScrape').checked;
  api('POST', '/api/config/steam', {
    auto_scrape: on,
  }).catch(e => {
    $('steamAutoScrape').checked = !on; // 保存失败回滚勾选
    showErrorModal('保存失败', e.message);
  });
}

async function triggerSteamScrape() {
  try {
    const resp = await api('POST', '/api/steam/scrape/trigger');
    if (resp.ok) {
      showModal('已触发', '<p>Steam 游戏榜单爬取已在后台开始，请查看日志。</p>', false, MODAL_AUTOCLOSE_MS);
    } else {
      showErrorModal('触发失败', resp.error || '未知错误');
    }
  } catch (e) {
    showErrorModal('触发失败', e.message);
  }
}

async function refreshSteamScrapeStatus() {
  try {
    const resp = await api('GET', '/api/steam/scrape/status');
    const emptyEl = $('steamScrapeStatusEmpty');
    const listEl = $('steamScrapeStatusList');
    if (!resp.ok || !resp.data || resp.data.length === 0) {
      if (emptyEl) emptyEl.textContent = '暂无榜单数据';
      if (listEl) listEl.style.display = 'none';
      if (emptyEl) emptyEl.style.display = '';
      return;
    }
    if (emptyEl) emptyEl.style.display = 'none';
    if (listEl) {
      let html = '';
      resp.data.forEach(item => {
        const dateDisplay = item.date ? String(item.date).replace(/-/g, '.') : '未爬取';
        const statusClass = item.need_update ? 'status-need-update' : 'status-ok';
        const statusText = item.need_update ? '需要更新' : '无需更新';
        html += `
          <div class="scrape-status-item">
            <div class="scrape-status-row">
              <span class="scrape-status-tag">${escapeHtml(String(item.tag || ''))}</span>
              <span class="scrape-status-state ${statusClass}">${statusText}</span>
            </div>
            <span class="scrape-status-meta">${escapeHtml(dateDisplay)} · ${escapeHtml(String(item.count == null ? 0 : item.count))}款</span>
          </div>`;
      });
      listEl.innerHTML = html;
      listEl.style.display = '';
    }
  } catch (e) {
    console.error('获取 Steam 榜单状态失败:', e);
  }
}

function onSteamVendorChange() {
  const vendor = $('steamVendorSelect').value;
  if (vendor && vendor !== '自定义' && STATE.providers[vendor]) {
    const info = STATE.providers[vendor];
    $('steamLLMBaseUrl').value = info.base_url || '';
    if (info.model) { $('steamLLMModel').value = info.model; }
  }
  restoreVendorKey('llm_steam', vendor, 'steamLLMApiKey');
}

async function saveSteamLLMConfig() {
  const maxTokens = parseInt($('steamLLMMaxTokens').value, 10) || 5000;
  try {
    await api('POST', '/api/config/llm_steam', {
      vendor: $('steamVendorSelect').value,
      api_key: $('steamLLMApiKey').value,
      base_url: $('steamLLMBaseUrl').value,
      model: $('steamLLMModel').value,
      api_path: '/chat/completions',
      max_tokens: maxTokens,
    });
    await loadConfig();
    showModal('已保存', '<p>Steam 评价模型配置已保存</p>', false, MODAL_AUTOCLOSE_MS);
  } catch (e) {
    showErrorModal('请求失败', e.message);
  }
}

async function fetchSteamModels() {
  const apiKey = $('steamLLMApiKey').value || $('apiKey').value;
  const baseUrl = $('steamLLMBaseUrl').value || $('baseUrl').value;
  if (!apiKey || !baseUrl) { showModal('缺少配置', '<p>请先填写 API Key 和 Base URL</p>'); return; }
  try {
    const resp = await api('POST', '/api/llm/models_for', { api_key: apiKey, base_url: baseUrl });
    if (resp.ok && resp.data && resp.data.length > 0) {
      const modelIds = resp.data;
      $('steamLLMModel').value = modelIds[0];
      showModal('模型列表', `<p>共 <strong>${modelIds.length}</strong> 个模型，第一个已填充</p>`, false, MODAL_AUTOCLOSE_MS);
    } else if (resp.ok) {
      showModal('获取失败', '<p>未获取到模型列表</p>');
    } else {
      showErrorModal('获取失败', resp.error);
    }
  } catch (e) { showErrorModal('请求失败', e.message); }
}

async function testSteamLLM() {
  await saveSteamLLMConfig();
  showModal('测试连接中...', '<p style="text-align:center;">正在测试 Steam 评价模型...</p>');
  const apiKey = $('steamLLMApiKey').value || $('apiKey').value;
  const baseUrl = $('steamLLMBaseUrl').value || $('baseUrl').value;
  const model = $('steamLLMModel').value || $('modelInput').value;
  if (!apiKey || !baseUrl || !model) { closeModal(); showModal('缺少配置', '<p>请先填写模型配置</p>'); return; }
  try {
    const resp = await api('POST', '/api/llm/test_for', { api_key: apiKey, base_url: baseUrl, model: model }, LLM_TEST_TIMEOUT_MS);
    if (resp.ok) {
      showModal('连接成功', `<div class="alert alert-info"><strong>模型：</strong>${escapeHtml(resp.model || model)}<br><strong>回复：</strong>${escapeHtml(resp.response || '(空)')}</div>`);
    } else {
      showErrorModal('连接失败', resp.error);
    }
  } catch (e) { showErrorModal('请求失败', e.message); }
}

function populateSteamVendors() {
  const select = $('steamVendorSelect');
  const currentValue = select.value;
  if (!findOption(select, '')) {
    const defaultOpt = document.createElement('option');
    defaultOpt.value = ''; defaultOpt.textContent = '-- 继承主模型 --';
    select.insertBefore(defaultOpt, select.firstChild);
  }
  Object.keys(STATE.providers).forEach(name => {
    if (!findOption(select, name)) {
      const opt = document.createElement('option'); opt.value = name; opt.textContent = name;
      select.appendChild(opt);
    }
  });
  select.value = currentValue;
}

// ==============================
// 配置保存
// ==============================

async function saveReasoningSilent() {
  try {
    await api('POST', '/api/config/llm', {
      show_reasoning: $('showReasoning').checked,
    });
  } catch (e) {
    $('showReasoning').checked = !$('showReasoning').checked; // 保存失败回滚勾选
    showErrorModal('保存失败', e.message);
  }
}

async function saveWebSearchSilent() {
  try {
    await api('POST', '/api/config/llm', {
      web_search: $('webSearch').checked,
    });
  } catch (e) {
    $('webSearch').checked = !$('webSearch').checked; // 保存失败回滚勾选
    $('searchModelSection').style.display = $('webSearch').checked ? 'block' : 'none';
    showErrorModal('保存失败', e.message);
  }
}

function onParallelToggle() {
  const on = $('parallelMode').checked;
  $('parallelSection').style.display = on ? 'block' : 'none';
  saveParallelSilent(true);
}

// fromToggle 区分触发来源：复选框切换失败才回滚勾选；
// 数量输入失败只恢复数量，不动复选框
function saveParallelSilent(fromToggle = false) {
  api('POST', '/api/config/bot', {
    parallel: $('parallelMode').checked,
    parallel_count: parseInt($('parallelCount').value, 10) || 5,
  }).catch(e => {
    if (fromToggle) {
      $('parallelMode').checked = !$('parallelMode').checked; // 保存失败回滚勾选
      $('parallelSection').style.display = $('parallelMode').checked ? 'block' : 'none';
    } else {
      // 数量保存失败：恢复为当前配置中的值
      const bot = STATE.currentConfig.bot || {};
      $('parallelCount').value = bot.parallel_count || 5;
    }
    showErrorModal('保存失败', e.message);
  });
}

// ==============================
// 多账号管理
// ==============================

function onMultiAccountToggle() {
  const on = $('multiAccount').checked;
  api('POST', '/api/config/bot', { multi_account: on }).catch(e => {
    $('multiAccount').checked = !on; // 保存失败回滚勾选
    showErrorModal('保存失败', e.message);
  });
}

async function refreshAltAccounts() {
  try {
    const resp = await api('GET', '/api/login/alt/accounts');
    const listEl = $('altAccountList');
    STATE.altAccounts = (resp.ok && resp.data) ? resp.data : [];
    if (STATE.altAccounts.length === 0) {
      listEl.innerHTML = '<span style="color:var(--text-secondary)">暂无副号，请添加</span>';
      updateStandbyUI();
      return;
    }
    let html = '';
    STATE.altAccounts.forEach(acc => {
      const avatarHtml = isSafeAvatarUrl(acc.avatar)
        ? `<img src="${escapeHtml(acc.avatar)}" style="width:22px;height:22px;border-radius:50%">`
        : '';
      html += `
        <div data-slot="${escapeHtml(String(acc.slot))}" style="display:flex;align-items:center;gap:8px;padding:4px 0;border-bottom:1px solid var(--border-light);margin-bottom:4px;">
          ${avatarHtml}
          <span style="flex:1">${escapeHtml(acc.nickname || '')} (ID:${escapeHtml(String(acc.heybox_id || ''))})</span>
          <label class="toggle-switch" style="flex-shrink:0" title="${acc.enabled !== false ? '已启用' : '已禁用'}">
            <input type="checkbox" class="alt-toggle" ${acc.enabled !== false ? 'checked' : ''}>
            <span class="toggle-slider"></span>
          </label>
          <button class="btn btn-ghost btn-sm alt-promote" style="font-size:10px;padding:2px 6px;" title="将该副号设为主号，原主号转为副号">设为主号</button>
          <button class="btn btn-ghost btn-sm alt-remove" style="color:var(--red);font-size:10px;padding:2px 6px;">移除</button>
        </div>`;
    });
    listEl.innerHTML = html;
    // 渲染后统一绑定事件，避免内联 onchange/onclick 字符串拼接
    listEl.querySelectorAll('[data-slot]').forEach(el => {
      const slot = el.getAttribute('data-slot');
      const toggle = el.querySelector('.alt-toggle');
      if (toggle) toggle.addEventListener('change', () => toggleAltAccount(slot, toggle.checked));
      const promoteBtn = el.querySelector('.alt-promote');
      if (promoteBtn) promoteBtn.addEventListener('click', () => promoteAltAccount(slot));
      const removeBtn = el.querySelector('.alt-remove');
      if (removeBtn) removeBtn.addEventListener('click', () => removeAltAccount(slot));
    });
    updateStandbyUI();
  } catch (e) {
    console.error('刷新副号列表失败:', e);
  }
}

async function showAltLoginModal() {
  try {
    const resp = await api('GET', '/api/login/alt/qrcode');
    if (!resp.ok) {
      showErrorModal('获取失败', resp.error);
      return;
    }
    const data = resp.data;
    showModal('扫码登录副号', `
      <div class="qr-container">
        <div id="altQrBox"></div>
        <div class="qr-status" id="altQrStatus">请使用小黑盒 APP 扫描（请切换账号）</div>
      </div>
    `);
    renderQRCode($('altQrBox'), data.qr_url);
    pollAltQRState(data.qr_id);
  } catch (e) {
    showErrorModal('请求失败', e.message);
  }
}

async function pollAltQRState(qrId) {
  const myId = ++STATE.qrPollId;
  const statusEl = $('altQrStatus');
  if (!statusEl) return;
  for (let retries = 0; retries < QR_POLL_MAX; retries++) {
    // 已有更新的轮询在进行：旧轮询作废
    if (myId !== STATE.qrPollId) return;
    // 弹窗已关闭：停止轮询
    if (statusEl.isConnected === false) return;
    try {
      const resp = await api('GET', `/api/login/alt/qrstate/${qrId}`);
      if (!resp.ok) {
        statusEl.textContent = '查询状态失败';
        return;
      }
      if (resp.state === 'ok') {
        closeModal();
        refreshAltAccounts();
        showModal('副号登录成功', `<div style="text-align:center;"><p>${escapeHtml(resp.nickname || '')} 已添加</p></div>`, false, MODAL_AUTOCLOSE_MS);
        return;
      } else if (resp.state === 'ready') {
        statusEl.textContent = '扫码成功，请在 APP 中确认登录...';
      } else if (resp.state === 'wait') {
        statusEl.textContent = '等待扫码...';
      } else {
        statusEl.textContent = `状态: ${resp.state}`;
        return;
      }
    } catch (e) {
      statusEl.textContent = '网络错误，稍后重试';
    }
    await sleep(QR_POLL_INTERVAL_MS);
  }
  if (myId !== STATE.qrPollId || statusEl.isConnected === false) return;
  statusEl.textContent = '二维码已超时';
}

async function removeAltAccount(slot) {
  showConfirmModal('移除副号', '<p>确定要移除这个副号吗？</p>', async () => {
    try {
      await api('DELETE', `/api/login/alt/accounts/${slot}`);
      refreshAltAccounts();

      showModal('已移除', '<p>副号已移除</p>', false, MODAL_AUTOCLOSE_MS);
    } catch (e) {
      showErrorModal('请求失败', e.message);
    }
  });
}

async function toggleAltAccount(slot, enabled) {
  try {
    await api('POST', `/api/login/alt/accounts/${slot}/toggle`, { enabled });
    refreshAltAccounts();
  } catch (e) {
    showErrorModal('请求失败', e.message);
    refreshAltAccounts(); // 失败后刷新，恢复真实勾选状态
  }
}

async function promoteAltAccount(slot) {
  const acc = (STATE.altAccounts || []).find(a => String(a.slot) === String(slot));
  const nickname = acc ? (acc.nickname || slot) : slot;
  showConfirmModal('设为主号', `
    <p>确定将副号「${escapeHtml(nickname)}」设为主号吗？</p>
    <p style="color:var(--text-secondary)">当前主号将自动转为副号；若该副号正作为替身，替身模式将自动关闭。需先停止机器人。</p>
  `, async () => {
    try {
      const resp = await api('POST', `/api/login/alt/accounts/${slot}/promote`);
      if (!resp.ok) {
        showErrorModal('切换失败', resp.error || '未知错误');
        return;
      }
      await checkLoginStatus();
      refreshAltAccounts();

      if (resp.standby_cleared) {
        $('standbyMode').checked = false;
        updateStandbyUI();
      }
      const newName = (resp.data && resp.data.nickname) || '';
      showModal('切换成功', `<p>「${escapeHtml(newName)}」已设为主号，原主号已转为副号</p>`, false, MODAL_AUTOCLOSE_MS);
    } catch (e) {
      showErrorModal('请求失败', e.message);
    }
  });
}

function onAutoDisableChange() {
  const val = $('autoDisableAltOnRisk').checked;
  api('POST', '/api/config/bot', { auto_disable_alt_on_risk: val }).catch(e => {
    $('autoDisableAltOnRisk').checked = !val; // 保存失败回滚勾选
    showErrorModal('保存失败', e.message);
  });
}

// ==============================
// 替身模式
// ==============================

function onStandbyToggle() {
  const on = $('standbyMode').checked;
  $('standbySlotGroup').style.display = on ? 'block' : 'none';
  if (on) {
    refreshStandbySlots();
    // 开启时立即落盘：不点"保存配置"也能生效（机器人按后端配置热更新）
    api('POST', '/api/config/bot', { standby_mode: true }).catch(e => {
      $('standbyMode').checked = false; // 保存失败回滚勾选
      $('standbySlotGroup').style.display = 'none';
      showErrorModal('保存失败', e.message);
    });
  } else {
    api('POST', '/api/config/bot', { standby_mode: false }).catch(e => {
      $('standbyMode').checked = true; // 保存失败回滚勾选
      $('standbySlotGroup').style.display = 'block';
      showErrorModal('保存失败', e.message);
    });
  }
}

function refreshStandbySlots() {
  const select = $('standbySlot');
  const currentVal = select.value;
  select.innerHTML = '<option value="">-- 请选择替身 --</option>';
  // 从副号列表缓存中提取 slot 和昵称
  (STATE.altAccounts || []).forEach(acc => {
    const opt = document.createElement('option');
    opt.value = acc.slot;
    opt.textContent = `${acc.nickname || ''} (ID:${acc.heybox_id || ''})`;
    select.appendChild(opt);
  });
  select.value = currentVal;
}

function onStandbySlotChange() {
  const slot = $('standbySlot').value;
  api('POST', '/api/config/bot', { standby_slot: slot }).catch(e => showErrorModal('保存失败', e.message));
}

function updateStandbyUI() {
  // 替身区块始终显示：副号为空时替身配置可能仍处于开启状态，
  // 隐藏会让用户误以为功能消失（如退出主号重新登录后副号被清空的场景）
  $('standbySection').style.display = 'block';
  $('standbySlotGroup').style.display = $('standbyMode').checked ? 'block' : 'none';
  if ($('standbyMode').checked) refreshStandbySlots();
}

async function saveBotConfig() {
  const botConfig = {
    mode: $('botMode').value,
    white_list: $('whiteList').value.split('\n').map(s => s.trim()).filter(s => s).map(s => parseInt(s, 10)).filter(n => !isNaN(n)),
    frequency: parseInt($('frequencyLimit').value, 10) || 3,
    init_wait_time: parseInt($('initWaitTime').value, 10) || 10,
    max_wait_time: parseInt($('maxWaitTime').value, 10) || 60,
    increment: parseInt($('increment').value, 10) || 10,
    max_messages_per_round: parseInt($('maxMessagesPerRound').value, 10) || 3,
    standby_mode: $('standbyMode').checked,
    standby_slot: $('standbySlot').value,
    auto_like: $('autoLike').checked,
  };

  // 正式保存并展示结果
  const doSave = async () => {
    const resp = await api('POST', '/api/config/bot', botConfig);
    if (!resp.ok) {
      showErrorModal('保存失败', resp.error || '未知错误');
      return;
    }
    await loadConfig();
    const warnings = resp.warnings || [];
    if (warnings.length > 0) {
      showModal('配置已保存（含修正）', buildWarningHtml(warnings));
    } else {
      showModal('配置已保存', '<p>机器人配置已成功更新。</p>', false, MODAL_AUTOCLOSE_MS);
    }
  };

  try {
    // 先 dry_run 校验，确认是否触发风控风险提示
    const dry = await api('POST', '/api/config/bot', { ...botConfig, dry_run: true });

    if (!dry.ok) {
      showErrorModal('保存失败', dry.error || '未知错误');
      return;
    }

    // 安全风险提示（后端返回的）：确认后才真正保存
    if (dry.risk_warning) {
      showConfirmModal(
        '安全风险警告',
        `<div class="alert alert-danger" style="font-size:14px;line-height:1.6;">
          <strong>警告：</strong>您设置的参数低于安全阈值，可能会触发小黑盒平台的反机器人风控机制，导致账号被限制或封禁。<br><br>
          是否确认使用该配置？
        </div>`,
        async () => {
          try {
            await doSave();
          } catch (e) {
            showErrorModal('请求失败', e.message);
          }
        },
        true
      );
      return;
    }

    // 无风险：直接保存
    try {
      await doSave();
    } catch (e) {
      showErrorModal('请求失败', e.message);
    }

  } catch (e) {
    showErrorModal('请求失败', e.message);
  }
}

function buildWarningHtml(warnings) {
  return `
    <div class="alert alert-warning">
      <strong>修正提示：</strong><br>
      ${warnings.map(w => `• ${escapeHtml(String(w))}`).join('<br>')}
    </div>
    <p class="small muted">配置已保存，请确认修正结果符合预期。</p>
  `;
}

async function saveLLMConfig() {
  const vendor = $('vendorSelect').value;
  const model = $('modelSelect').value || $('modelInput').value;

  const maxTokens = parseInt($('maxTokens').value, 10) || 5000;
  const llmConfig = {
    vendor: vendor,
    api_key: $('apiKey').value,
    base_url: $('baseUrl').value,
    api_path: $('apiPath').value || '/chat/completions',
    model: model,
    max_tokens: maxTokens,
    web_search: $('webSearch').checked,
  };

  try {
    const resp = await api('POST', '/api/config/llm', llmConfig);
    if (resp.ok) {
      showModal('保存成功', '<p>AI 模型配置已保存</p>', false, MODAL_AUTOCLOSE_MS);
      await loadConfig();
    } else {
      showErrorModal('保存失败', resp.error);
    }
  } catch (e) {
    showErrorModal('请求失败', e.message);
  }
}

async function savePrompt() {
  const content = $('promptContent').value;
  try {
    await api('POST', '/api/config/prompt', { content });
    showModal('保存成功', '<p>提示词已更新</p>', false, MODAL_AUTOCLOSE_MS);
  } catch (e) {
    showErrorModal('请求失败', e.message);
  }
}

async function resetPrompt() {
  showConfirmModal('恢复默认提示词', '<p>确定要恢复为默认提示词吗？当前自定义提示词将被覆盖。</p>', async () => {
    try {
      const resp = await api('POST', '/api/config/prompt/reset');
      if (resp.ok) {
        $('promptContent').value = resp.content || '';
        showModal('已恢复', '<p>提示词已恢复为默认</p>', false, MODAL_AUTOCLOSE_MS);
      } else {
        showErrorModal('恢复失败', resp.error);
      }
    } catch (e) {
      showErrorModal('请求失败', e.message);
    }
  });
}

async function loadSteamPrompt() {
  try {
    const resp = await api('GET', '/api/config/steam_prompt');
    if (resp.ok) {
      $('steamPromptContent').value = resp.content || '';
    }
  } catch (e) {
    console.error('加载 Steam 提示词失败:', e);
  }
}

async function saveSteamPrompt() {
  const content = $('steamPromptContent').value;
  try {
    await api('POST', '/api/config/steam_prompt', { content });
    showModal('保存成功', '<p>Steam 库存评价提示词已更新</p>', false, MODAL_AUTOCLOSE_MS);
  } catch (e) {
    showErrorModal('请求失败', e.message);
  }
}

async function resetSteamPrompt() {
  showConfirmModal('恢复默认', '<p>确定要恢复为默认 Steam 评价提示词吗？</p>', async () => {
    try {
      const resp = await api('POST', '/api/config/steam_prompt/reset');
      if (resp.ok) {
        $('steamPromptContent').value = resp.content || '';
        showModal('已恢复', '<p>Steam 评价提示词已恢复为默认</p>', false, MODAL_AUTOCLOSE_MS);
      } else {
        showErrorModal('恢复失败', resp.error);
      }
    } catch (e) {
      showErrorModal('请求失败', e.message);
    }
  });
}

async function loadSteamRecPrompt() {
  try {
    const resp = await api('GET', '/api/config/steam_recommend_prompt');
    if (resp.ok) {
      $('steamRecPromptContent').value = resp.content || '';
    }
  } catch (e) {
    console.error('加载 Steam 推荐提示词失败:', e);
  }
}

async function saveSteamRecPrompt() {
  const content = $('steamRecPromptContent').value;
  try {
    await api('POST', '/api/config/steam_recommend_prompt', { content });
    showModal('保存成功', '<p>Steam 推荐游戏提示词已更新</p>', false, MODAL_AUTOCLOSE_MS);
  } catch (e) {
    showErrorModal('请求失败', e.message);
  }
}

async function resetSteamRecPrompt() {
  showConfirmModal('恢复默认', '<p>确定要恢复为默认 Steam 推荐提示词吗？</p>', async () => {
    try {
      const resp = await api('POST', '/api/config/steam_recommend_prompt/reset');
      if (resp.ok) {
        $('steamRecPromptContent').value = resp.content || '';
        showModal('已恢复', '<p>Steam 推荐提示词已恢复为默认</p>', false, MODAL_AUTOCLOSE_MS);
      } else {
        showErrorModal('恢复失败', resp.error);
      }
    } catch (e) {
      showErrorModal('请求失败', e.message);
    }
  });
}

// ==============================
// API Key 操作
// ==============================

function toggleApiKey(id = 'apiKey') {
  const input = $(id);
  input.type = input.type === 'password' ? 'text' : 'password';
}

async function copyApiKey(id = 'apiKey') {
  const input = $(id);
  // 优先使用 Clipboard API，失败或不存在时回退 execCommand
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(input.value);
      return;
    }
  } catch (e) {
    // 继续走回退方案
  }
  const oldType = input.type;
  input.type = 'text';
  input.select();
  document.execCommand('copy');
  input.type = oldType;
}

// 页脚 SERVER 地址复制：优先 Clipboard API，降级临时 textarea + execCommand
async function copyServerAddr() {
  const valueEl = $('serverAddr');
  const text = window.location.origin || 'http://127.0.0.1:5500';
  const showCopied = () => {
    if (!valueEl) return;
    const old = valueEl.textContent;
    valueEl.textContent = '已复制 ✓';
    setTimeout(() => { valueEl.textContent = old; }, 1500);
  };
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      showCopied();
      return;
    }
  } catch (e) {
    // 继续走回退方案
  }
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand('copy'); } catch (e) { /* 忽略 */ }
  document.body.removeChild(ta);
  showCopied();
}

// ==============================
// LLM 操作
// ==============================

function filterSelectOptions(inputEl, selectEl) {
  const filter = inputEl.value.toLowerCase();
  const options = selectEl.querySelectorAll('option');
  options.forEach(opt => {
    if (opt.value === '') return; // 保留"请选择"选项
    const text = opt.textContent.toLowerCase();
    opt.style.display = text.includes(filter) ? '' : 'none';
  });
}

async function fetchModels() {
  try {
    await saveLLMConfigSilent();
    const resp = await api('GET', '/api/llm/models');
    if (resp.ok && resp.data) {
      const select = $('modelSelect');
      select.innerHTML = '<option value="">-- 选择模型 --</option>';
      resp.data.forEach(m => {
        const opt = document.createElement('option');
        opt.value = m;
        opt.textContent = m;
        select.appendChild(opt);
      });
      showModal('模型列表已更新', `
        <p>共获取到 <strong>${resp.data.length}</strong> 个模型，请在下拉框中搜索选择</p>
        <input type="text" placeholder="搜索模型..." style="width:100%;margin-top:8px;padding:8px;background:var(--bg-input);border:1px solid var(--border);border-radius:6px;color:var(--text-primary);"
          oninput="filterSelectOptions(this, $('modelSelect'))">
        <p class="small mute" style="margin-top:4px;">或直接在下方输入框中手动输入模型名称</p>
      `, false);
    } else {
      showErrorModal('获取失败', resp.error);
    }
  } catch (e) {
    showErrorModal('请求失败', e.message);
  }
}

async function saveLLMConfigSilent() {
  const vendor = $('vendorSelect').value;
  const model = $('modelSelect').value || $('modelInput').value;
  const maxTokens = parseInt($('maxTokens').value, 10) || 5000;
  await api('POST', '/api/config/llm', {
    vendor, model,
    api_key: $('apiKey').value,
    base_url: $('baseUrl').value,
    api_path: $('apiPath').value || '/chat/completions',
    max_tokens: maxTokens,
    web_search: $('webSearch').checked,
  });
}

async function saveSearchConfig() {
  try {
    await api('POST', '/api/config/llm_search', {
      vendor: $('searchVendorSelect').value,
      api_key: $('searchApiKey').value,
      base_url: $('searchBaseUrl').value,
      model: $('searchModelInput').value,
      api_path: '/chat/completions',
      max_tokens: parseInt($('searchMaxTokens').value, 10) || 5000,
    });
    // 同时保存搜索 API 配置
    await api('POST', '/api/config/llm_baidu_search', {
      baidu_api_key: $('baiduApiKey').value,
      tavily_api_key: $('tavilyApiKey').value,
      model: $('baiduModelSelect').value,
      provider: $('searchProviderSelect').value,
    });
    await loadConfig();
  } catch (e) {
    showErrorModal('请求失败', e.message);
  }
}

async function saveSearchAPIOnly() {
  try {
    await api('POST', '/api/config/llm_baidu_search', {
      baidu_api_key: $('baiduApiKey').value,
      tavily_api_key: $('tavilyApiKey').value,
      model: $('baiduModelSelect').value,
      provider: $('searchProviderSelect').value,
    });
    await loadConfig();
    showModal('已保存', '<p>搜索 API 配置已更新</p>', false, MODAL_AUTOCLOSE_MS);
  } catch (e) {
    showErrorModal('请求失败', e.message);
  }
}

async function testSearchAPI() {
  await saveSearchAPIOnly();
  showModal('测试搜索中...', '<p style="text-align:center;">正在搜索"今日热点"，请稍候...</p>');
  try {
    const resp = await api('POST', '/api/search/test', null, LLM_TEST_TIMEOUT_MS);
    if (resp.ok) {
      showInfoModal('搜索测试成功', '搜索 "今日热点" 结果：\n' + (resp.response || '').replace(/\[SEARCH\]/g, ''));
    } else {
      showErrorModal('搜索测试失败', resp.error);
    }
  } catch (e) {
    showErrorModal('搜索测试出错', e.message);
  }
}

function populateSearchVendors() {
  const select = $('searchVendorSelect');
  const currentValue = select.value;
  if (!findOption(select, '')) {
    const defaultOpt = document.createElement('option');
    defaultOpt.value = '';
    defaultOpt.textContent = '-- 继承主模型 --';
    select.insertBefore(defaultOpt, select.firstChild);
  }
  Object.keys(STATE.providers).forEach(name => {
    if (!findOption(select, name)) {
      const opt = document.createElement('option');
      opt.value = name;
      opt.textContent = name;
      select.appendChild(opt);
    }
  });
  select.value = currentValue;
}

async function fetchSearchModels() {
  const apiKey = $('searchApiKey').value || $('apiKey').value;
  const baseUrl = $('searchBaseUrl').value || $('baseUrl').value;

  if (!apiKey || !baseUrl) {
    showModal('缺少配置', '<p>请先填写搜索模型的 API Key 和 Base URL</p>');
    return;
  }

  try {
    const resp = await api('POST', '/api/llm/models_for', { api_key: apiKey, base_url: baseUrl });
    if (resp.ok && resp.data && resp.data.length > 0) {
      const modelIds = resp.data;
      $('searchModelInput').value = modelIds[0];
      // 弹出下拉让用户选择
      const searchSelectId = 'searchModelPopupSelect_' + Date.now();
      showModal('搜索模型列表', `
        <p>共获取到 <strong>${modelIds.length}</strong> 个模型，第一个已自动填充。</p>
        <input type="text" placeholder="搜索模型..." style="width:100%;margin-top:8px;padding:8px;background:var(--bg-input);border:1px solid var(--border);border-radius:6px;color:var(--text-primary);"
          oninput="filterSelectOptions(this, document.getElementById('${searchSelectId}'))">
        <select id="${searchSelectId}" style="width:100%;margin-top:8px;" onchange="$('searchModelInput').value=this.value;closeModal()"></select>
      `);
      const select = $(searchSelectId);
      modelIds.forEach(m => {
        const opt = document.createElement('option');
        opt.value = m;
        opt.textContent = m;
        select.appendChild(opt);
      });
    } else if (resp.ok) {
      showModal('获取失败', '<p>未获取到模型列表</p>');
    } else {
      showErrorModal('获取失败', resp.error);
    }
  } catch (e) {
    showErrorModal('请求失败', e.message);
  }
}

async function testSearchLLM() {
  await saveSearchConfig();
  showModal('测试搜索连接中...', '<p style="text-align:center;">正在测试搜索关键词模型连接，请稍候...</p>');
  try {
    const resp = await api('POST', '/api/llm/test_search', null, LLM_TEST_TIMEOUT_MS);
    if (resp.ok) {
      showModal('搜索连接成功', `
        <div class="alert alert-info">
          <strong>模型：</strong>${escapeHtml(resp.model || '未知')}<br>
          <strong>回复：</strong>${escapeHtml(resp.response || '')}
        </div>
      `);
    } else {
      showErrorModal('搜索连接失败', resp.error);
    }
  } catch (e) {
    showErrorModal('搜索连接失败', e.message);
  }
}

async function resetAllConfig() {
  showConfirmModal(
    '重置所有配置',
    `<div class="alert alert-danger" style="font-size:14px;line-height:1.6;">
      <strong>确定要重置所有配置吗？此操作不可恢复！</strong><br><br>
      所有 AI 模型配置、机器人参数、提示词、白名单将被恢复为默认值。<br>
      <span style="color: var(--accent-green)">登录状态不受影响。</span>
    </div>`,
    async () => {
      try {
        const resp = await api('POST', '/api/config/reset');
        if (resp.ok) {
          await loadConfig();
          showModal('已重置', '<p>所有配置已恢复为默认值</p>', false, MODAL_AUTOCLOSE_MS);
        } else {
          showErrorModal('重置失败', resp.error || '未知错误');
        }
      } catch (e) {
        showErrorModal('请求失败', e.message);
      }
    },
    true
  );
}

async function testLLM() {
  showModal('测试连接中...', '<p style="text-align:center;">正在测试 AI 模型连接，请稍候...</p>');

  try {
    await saveLLMConfigSilent();
    const resp = await api('POST', '/api/llm/test', null, LLM_TEST_TIMEOUT_MS);
    if (resp.ok) {
      showModal('连接成功', `
        <div class="alert alert-info">
          <strong>模型：</strong>${escapeHtml(resp.model || '未知')}<br>
          <strong>回复：</strong>${escapeHtml(resp.response || '')}
        </div>
      `);
    } else {
      showErrorModal('连接失败', resp.error);
    }
  } catch (e) {
    showErrorModal('请求失败', e.message);
  }
}

// ==============================
// 机器人控制
// ==============================

async function startBot() {
  try {
    const resp = await api('POST', '/api/bot/start');
    if (resp.ok) {
      // 立即向后端确认真实状态，避免状态被异步刷新覆盖
      await refreshBotStatus();
      if (STATE.botRunning) {
        updateBotUI();
        showModal('机器人已启动', '<p>AI 自动回复机器人已开始运行。</p>', false, MODAL_AUTOCLOSE_MS);
      } else {
        showModal('启动未生效', '<p>后端未报告运行状态，请检查日志。</p>');
      }
    } else {
      showErrorModal('启动失败', resp.error || '未知错误');
    }
  } catch (e) {
    showErrorModal('启动出错', e.message);
  }
}

async function stopBot() {
  try {
    const resp = await api('POST', '/api/bot/stop');
    if (!resp.ok) {
      showErrorModal('停止失败', resp.error || '未知错误');
      return;
    }
    STATE.botRunning = false;
    updateBotUI();
  } catch (e) {
    showErrorModal('停止机器人失败', e.message);
  }
}

function updateBotUI() {
  const pulse = $('statusPulse');
  const label = $('statusLabel');
  const badge = $('missionBadge');
  const card = $('missionCard');
  const btn = $('missionButton');
  const btnIcon = $('missionButtonIcon');
  const btnText = $('missionButtonText');
  const countdown = $('missionCountdown');

  if (STATE.botRunning) {
    pulse.className = 'status-pulse online';
    label.textContent = '在线';
    badge.textContent = 'RUNNING';
    card.classList.add('running');
    btn.classList.add('running');
    btnIcon.textContent = '■';
    btnText.textContent = '停止机器人';
    countdown.style.display = 'flex';
  } else {
    pulse.className = 'status-pulse offline';
    label.textContent = '离线';
    badge.textContent = 'STANDBY';
    card.classList.remove('running');
    btn.classList.remove('running');
    btnIcon.textContent = '▶';
    btnText.textContent = '启动机器人';
    countdown.style.display = 'none';
    $('countdownValue').textContent = '--';
    STATE.waitTotal = 0;
  }
}

function onMissionButtonClick() {
  if (!STATE.loggedIn) {
    showModal('未登录', `
      <div class="alert alert-warning">
        请先登录小黑盒账号，再启动机器人。
      </div>
      <button class="btn btn-primary" onclick="closeModal();showLoginModal();" style="width:100%">立即登录</button>
    `);
    return;
  }
  if (STATE.botRunning) {
    stopBot();
  } else {
    startBot();
  }
}

async function refreshBotStatus() {
  try {
    const resp = await api('GET', '/api/bot/status');
    if (resp.ok && resp.data) {
      STATE.botRunning = resp.data.running;
      updateBotUI();
      if (resp.data.running && resp.data.wait_remaining !== undefined) {
        STATE.waitTotal = resp.data.wait_total;
        STATE.waitStartTime = Date.now() / 1000 - (STATE.waitTotal - resp.data.wait_remaining);
      } else {
        STATE.waitTotal = 0;
      }
    }
  } catch (e) {
    // 静默失败
  }
}

function startCountdownTimer() {
  if (STATE.countdownTimer) clearInterval(STATE.countdownTimer);
  STATE.countdownTimer = setInterval(() => {
    if (!STATE.botRunning || STATE.waitTotal <= 0) {
      const cv = $('countdownValue');
      if (cv && cv.textContent !== '--') cv.textContent = '--';
      return;
    }
    const elapsed = Date.now() / 1000 - STATE.waitStartTime;
    const remaining = Math.max(0, Math.ceil(STATE.waitTotal - elapsed));
    const cv = $('countdownValue');
    if (cv) cv.textContent = `${remaining}s`;
    if (remaining <= 0) STATE.waitTotal = 0;
  }, 1000);
}

// ==============================
// 日志相关
// ==============================

function initLogStream() {
  if (STATE.eventSource) {
    STATE.eventSource.close();
  }

  const es = new EventSource('/api/log/stream');
  STATE.eventSource = es;

  // SSE 连接后会先推送最近历史日志，第一条消息到达时清空占位
  let isFirstMessage = true;

  es.onmessage = function (event) {
    try {
      const entry = JSON.parse(event.data);
      if (isFirstMessage) {
        clearLogContainer();
        isFirstMessage = false;
        _sseReconnectDelay = SSE_RECONNECT_MS; // 连接成功，重置退避
        const ts = $('terminalStatus');
        if (ts) ts.textContent = '已连接';
      }
      appendLogEntry(entry);
    } catch (e) {
      // heartbeat
    }
  };

  es.onerror = function () {
    es.close();
    STATE.eventSource = null;
    const ts = $('terminalStatus');
    if (ts) ts.textContent = '重连中';
    // 指数退避：3s→6s→12s…封顶 30s
    const delay = _sseReconnectDelay;
    _sseReconnectDelay = Math.min(_sseReconnectDelay * 2, SSE_RECONNECT_MAX_MS);
    setTimeout(initLogStream, delay);
  };
}

function clearLogContainer(keepPlaceholder = false) {
  const container = $('logContainer');
  container.innerHTML = '';
}

const _tagColors = {};
const _tagPalette = ['#4ea1f0', '#4ec9b0', '#dcdcaa', '#c586c0', '#ce9178', '#569cd6', '#6a9955', '#d16969'];

// UID 颜色系统：每个 user_id 通过哈希映射到固定颜色，同一用户始终同色
const _uidPalette = [
  '#ff6b6b', '#51cf66', '#339af0', '#fcc419',
  '#cc5de8', '#20c997', '#ff922b', '#f06595',
  '#22b8cf', '#ffd43b', '#a9e34b', '#da77f2',
];
const _uidCache = {};
function _getUidColor(uid) {
  uid = String(uid); // 后端可能返回数字型 user_id，统一转字符串再哈希
  if (!_uidCache[uid]) {
    let hash = 0;
    for (let i = 0; i < uid.length; i++) {
      hash = ((hash << 5) - hash) + uid.charCodeAt(i);
      hash |= 0;
    }
    _uidCache[uid] = _uidPalette[Math.abs(hash) % _uidPalette.length];
  }
  return _uidCache[uid];
}

function _getTagColor(tag) {
  if (!_tagColors[tag]) {
    _tagColors[tag] = _tagPalette[Object.keys(_tagColors).length % _tagPalette.length];
  }
  return _tagColors[tag];
}

function _extractLogTag(msgText) {
  const m = msgText.match(/^\[([^\]]+)\]/);
  return m ? m[1] : '';
}

function appendLogEntry(entry) {
  const container = $('logContainer');
  const levelClass = entry.level === 'ERROR' ? 'log-error' : entry.level === 'WARN' ? 'log-warn' : 'log-info';

  const div = document.createElement('div');
  div.className = `log-entry ${levelClass}`;
  const msg = escapeHtml(entry.message);
  const logTime = escapeHtml(entry.timestamp || '--:--:--');
  const logLevel = escapeHtml(String(entry.level || 'INFO'));

  // 移除旧光标
  const oldCursor = container.querySelector('.log-cursor');
  if (oldCursor) oldCursor.remove();

  // 提取标签用于颜色和过滤
  const rawMsg = entry.message || '';
  const tag = _extractLogTag(rawMsg);
  if (tag) {
    div.setAttribute('data-tag', tag);
  }

  const uidTag = entry.user_id
    ? `<span style="color:${_getUidColor(entry.user_id)};font-weight:700;font-size:10px">[uid:${escapeHtml(entry.user_id)}]</span>`
    : '';

  // Steam 爬取日志：连续多条合并为一个可展开/收起的折叠组
  if (msg.startsWith('[Steam 爬取]')) {
    const firstEntry = container.querySelector('.log-entry');
    if (firstEntry && firstEntry.classList.contains('steam-fold-group')) {
      _appendSteamFoldLine(firstEntry, entry);
    } else {
      _createSteamFoldGroup(div, entry, levelClass, uidTag);
      const cursor = container.querySelector('.log-cursor');
      if (cursor) cursor.remove();
      container.insertBefore(div, firstEntry || null);
      const newCursor = document.createElement('span');
      newCursor.className = 'log-cursor';
      div.appendChild(newCursor);
    }
    return;
  }

  // 自检 Steam 榜单时效日志：前缀折叠，详情可展开
  const steamCheckPrefix = '[自检] 正在检测 Steam 游戏榜单时效...';
  if (msg.startsWith(steamCheckPrefix)) {
    const detail = msg.substring(steamCheckPrefix.length).trim();
    const tag = _extractLogTag(entry.message);
    if (tag) div.setAttribute('data-tag', tag);
    div.innerHTML = `
      <span class="log-prompt">$</span>
      <span class="log-time">${logTime}</span>
      <span class="log-level">[${logLevel}]</span>
      ${uidTag}
      <span class="log-msg search-log" ${tag ? `data-tag-color="${_getTagColor(tag)}"` : ''}>
        <span>${steamCheckPrefix}</span>
        <span class="search-toggle" onclick="toggleSearch(this)">▶</span>
        <span class="search-body" style="display:none">${detail.replace(/\n/g, '<br>')}</span>
      </span>
    `;
    _applyLogFilter(div);
    // 倒序插入
    const cursor = container.querySelector('.log-cursor');
    if (cursor) cursor.remove();
    const firstEntry = container.querySelector('.log-entry');
    if (firstEntry) {
      container.insertBefore(div, firstEntry);
    } else {
      container.appendChild(div);
    }
    const newCursor = document.createElement('span');
    newCursor.className = 'log-cursor';
    div.appendChild(newCursor);
    // 限制日志条数
    while (container.children.length > LOG_MAX_ENTRIES) {
      container.removeChild(container.lastChild);
    }
    return;
  }

  // 检测搜索结果标记 [SEARCH]
  if (msg.startsWith('[SEARCH]')) {
    const content = msg.substring('[SEARCH]'.length);
    div.innerHTML = `
      <span class="log-prompt">$</span>
      <span class="log-time">${logTime}</span>
      <span class="log-level">[${logLevel}]</span>
      ${uidTag}
      <span class="log-msg search-log">
        <span class="search-toggle" onclick="toggleSearch(this)">[SEARCH] 展开搜索结果 ▶</span>
        <span class="search-body" style="display:none">${content.replace(/\n/g, '<br>')}</span>
      </span>
    `;
  } else if (rawMsg.includes('AI 回复成功') && msg.length > 120) {
    // AI 回复成功：完整文本可折叠
    const colonIdx = msg.indexOf(':');
    const summary = colonIdx > 0 ? msg.substring(0, colonIdx + 1) + ' ...' : msg.substring(0, 100) + '...';
    div.innerHTML = `
      <span class="log-prompt">$</span>
      <span class="log-time">${logTime}</span>
      <span class="log-level">[${logLevel}]</span>
      ${uidTag}
      <span class="log-msg search-log">
        <span>${summary}</span>
        <span class="search-toggle" onclick="toggleSearch(this)">[REPLY] 展开完整回复 ▶</span>
        <span class="search-body" style="display:none">${msg.replace(/\n/g, '<br>')}</span>
      </span>`;
  } else {
    div.innerHTML = `
      <span class="log-prompt">$</span>
      <span class="log-time">${logTime}</span>
      <span class="log-level">[${logLevel}]</span>
      ${uidTag}
      <span class="log-msg" ${tag ? `data-tag-color="${_getTagColor(tag)}"` : ''}>${msg}</span>
    `;
  }

  // 给有标签的日志行添加颜色标识
  if (tag && div.querySelector('.log-msg')) {
    const color = _getTagColor(tag);
    const msgEl = div.querySelector('.log-msg');
    if (!msgEl.hasAttribute('data-tag-color')) {
      msgEl.style.borderLeft = `2px solid ${color}`;
      msgEl.style.paddingLeft = '8px';
    }
  }

  // 应用当前过滤器
  _applyLogFilter(div);

  // 倒序插入（最新在上）
  const firstEntry = container.querySelector('.log-entry');
  if (firstEntry) {
    container.insertBefore(div, firstEntry);
  } else {
    container.appendChild(div);
  }

  // 追加新光标到第一条日志
  const cursor = document.createElement('span');
  cursor.className = 'log-cursor';
  div.appendChild(cursor);

  // 限制日志条数（保留最近若干条）
  while (container.children.length > LOG_MAX_ENTRIES) {
    container.removeChild(container.lastChild);
  }
}

function _createSteamFoldGroup(div, entry, levelClass, uidTag) {
  div.className = `log-entry ${levelClass} steam-fold-group`;
  const msg = escapeHtml(entry.message);
  const logTime = escapeHtml(entry.timestamp || '--:--:--');
  const logLevel = escapeHtml(String(entry.level || 'INFO'));
  const tag = _extractLogTag(entry.message);
  if (tag) div.setAttribute('data-tag', tag);
  div.innerHTML = `
    <span class="log-prompt">$</span>
    <span class="log-time">${logTime}</span>
    <span class="log-level">[${logLevel}]</span>
    ${uidTag}
    <span class="log-msg search-log" ${tag ? `data-tag-color="${_getTagColor(tag)}"` : ''}>
      <span class="search-toggle" onclick="toggleSearch(this)">[Steam 爬取] 展开 1 条 ▶</span>
      <span class="search-body" style="display:none">
        <div class="steam-fold-line"><span class="log-time">${logTime}</span> ${msg}</div>
      </span>
    </span>
  `;
  div.setAttribute('data-steam-count', '1');
  _applyLogFilter(div);
}

function _appendSteamFoldLine(group, entry) {
  const body = group.querySelector('.search-body');
  const toggle = group.querySelector('.search-toggle');
  if (!body || !toggle) return;
  const msg = escapeHtml(entry.message);
  const line = document.createElement('div');
  line.className = 'steam-fold-line';
  line.innerHTML = `<span class="log-time">${escapeHtml(entry.timestamp || '--:--:--')}</span> ${msg}`;
  const firstEntry = body.querySelector('.steam-fold-line');
  if (firstEntry) {
    body.insertBefore(line, firstEntry);
  } else {
    body.appendChild(line);
  }
  const count = parseInt(group.getAttribute('data-steam-count') || '0', 10) + 1;
  group.setAttribute('data-steam-count', String(count));
  toggle.textContent = `[Steam 爬取] 展开 ${count} 条 ▶`;
  _applyLogFilter(group);
}

function onLogFilter() {
  const filter = ($('logFilter').value || '').trim().toLowerCase();
  const container = $('logContainer');
  const entries = container.querySelectorAll('.log-entry');
  entries.forEach(entry => {
    if (!filter) {
      entry.style.display = '';
    } else {
      const tag = (entry.getAttribute('data-tag') || '').toLowerCase();
      const msg = (entry.querySelector('.log-msg')?.textContent || '').toLowerCase();
      entry.style.display = (tag.includes(filter) || msg.includes(filter)) ? '' : 'none';
    }
  });
}

function _applyLogFilter(entry) {
  const filter = ($('logFilter').value || '').trim().toLowerCase();
  if (!filter) return;
  const tag = (entry.getAttribute('data-tag') || '').toLowerCase();
  const msg = (entry.querySelector('.log-msg')?.textContent || '').toLowerCase();
  entry.style.display = (tag.includes(filter) || msg.includes(filter)) ? '' : 'none';
}

function toggleSearch(el) {
  const body = el.parentElement.querySelector('.search-body');
  const expanded = el.dataset.expanded === '1';
  if (expanded) {
    body.style.display = 'none';
    el.dataset.expanded = '0';
    el.textContent = el.textContent.replace('收起', '展开').replace('▼', '▶');
  } else {
    body.style.display = 'block';
    el.dataset.expanded = '1';
    el.textContent = el.textContent.replace('展开', '收起').replace('▶', '▼');
  }
}

function toggleFold(header) {
  const fold = header.closest('.fold');
  const body = fold.querySelector('.fold-body');
  const isExpanded = fold.classList.toggle('expanded');
  body.style.display = isExpanded ? 'block' : 'none';
}

async function clearLogs() {
  showConfirmModal('清空日志', '<p>确定要清空所有日志吗？</p>', async () => {
    try {
      await api('POST', '/api/log/clear');
      $('logContainer').innerHTML = '';
    } catch (e) {
      showErrorModal('清空失败', e.message);
    }
  });
}

function exportLogs() {
  window.open('/api/log/export', '_blank');
}

function onSearchJudgeToggle() {
  const on = $('searchJudgeEnabled').checked;
  $('searchJudgeSection').style.display = on ? 'block' : 'none';
  api('POST', '/api/config/llm_search_judge', {
    enabled: on,
  }).catch(e => {
    $('searchJudgeEnabled').checked = !on; // 保存失败回滚勾选
    $('searchJudgeSection').style.display = !on ? 'block' : 'none';
    showErrorModal('保存失败', e.message);
  });
}

function onJudgeVendorChange() {
  const vendor = $('judgeVendorSelect').value;
  if (vendor && vendor !== '自定义' && STATE.providers[vendor]) {
    const info = STATE.providers[vendor];
    $('judgeBaseUrl').value = info.base_url || '';
    if (info.model) {
      $('judgeModelInput').value = info.model;
    }
  }
  // 从 vendor_keys 恢复该提供商的 API Key
  restoreVendorKey('llm_search_judge', vendor, 'judgeApiKey');
}

function populateJudgeVendors() {
  const select = $('judgeVendorSelect');
  const currentValue = select.value;
  Object.keys(STATE.providers).forEach(name => {
    if (!findOption(select, name)) {
      const opt = document.createElement('option');
      opt.value = name;
      opt.textContent = name;
      select.appendChild(opt);
    }
  });
  select.value = currentValue;
}

async function saveSearchJudgeConfig() {
  try {
    await api('POST', '/api/config/llm_search_judge', {
      enabled: $('searchJudgeEnabled').checked,
      vendor: $('judgeVendorSelect').value,
      api_key: $('judgeApiKey').value,
      base_url: $('judgeBaseUrl').value,
      model: $('judgeModelInput').value,
      max_tokens: 200,
    });
    await loadConfig();
  } catch (e) {
    showErrorModal('请求失败', e.message);
  }
}

async function fetchJudgeModels() {
  const apiKey = $('judgeApiKey').value;
  const baseUrl = $('judgeBaseUrl').value;
  if (!apiKey || !baseUrl) {
    showModal('缺少配置', '<p>请先填写判断模型的 API Key 和 Base URL</p>');
    return;
  }
  try {
    const resp = await api('POST', '/api/llm/models_for', { api_key: apiKey, base_url: baseUrl });
    if (resp.ok && resp.data && resp.data.length > 0) {
      const modelIds = resp.data;
      $('judgeModelInput').value = modelIds[0];
      const jSelectId = 'judgeModelPopupSelect_' + Date.now();
      showModal('判断模型列表', `
        <p>共 <strong>${modelIds.length}</strong> 个模型，第一个已自动填充。</p>
        <input type="text" placeholder="搜索模型..." style="width:100%;margin-top:8px;padding:8px;background:var(--bg-input);border:1px solid var(--border);border-radius:6px;color:var(--text-primary);"
          oninput="filterSelectOptions(this, document.getElementById('${jSelectId}'))">
        <select id="${jSelectId}" style="width:100%;margin-top:8px;" onchange="$('judgeModelInput').value=this.value;closeModal()"></select>
      `);
      const select = $(jSelectId);
      modelIds.forEach(m => {
        const opt = document.createElement('option');
        opt.value = m;
        opt.textContent = m;
        select.appendChild(opt);
      });
    } else if (resp.ok) {
      showModal('获取失败', '<p>未获取到模型列表</p>');
    } else {
      showErrorModal('获取失败', resp.error);
    }
  } catch (e) {
    showErrorModal('请求失败', e.message);
  }
}

async function testJudgeLLM() {
  await saveSearchJudgeConfig();
  showModal('测试判断连接中...', '<p style="text-align:center;">正在测试判断模型连接，请稍候...</p>');
  try {
    const resp = await api('POST', '/api/llm/test_judge', null, LLM_TEST_TIMEOUT_MS);
    if (resp.ok) {
      showModal('判断连接成功', `<div class="alert alert-info"><strong>模型：</strong>${escapeHtml(resp.model || '未知')}<br><strong>回复：</strong>${escapeHtml(resp.response || '')}</div>`);
    } else {
      showErrorModal('判断连接失败', resp.error);
    }
  } catch (e) {
    showErrorModal('判断连接失败', e.message);
  }
}

function onSearchProviderChange() {
  const provider = $('searchProviderSelect').value;
  if (provider === 'auto') {
    // 自动故障转移：两个 Key 输入框都显示
    $('baiduModelGroup').style.display = '';
    $('baiduKeyGroup').style.display = '';
    $('tavilyKeyGroup').style.display = '';
    $('btnBaiduConsole').style.display = '';
    $('btnTavilyConsole').style.display = '';
  } else if (provider === 'tavily') {
    $('baiduModelGroup').style.display = 'none';
    $('baiduKeyGroup').style.display = 'none';
    $('tavilyKeyGroup').style.display = '';
    $('btnBaiduConsole').style.display = 'none';
    $('btnTavilyConsole').style.display = '';
  } else {
    // baidu
    $('baiduModelGroup').style.display = '';
    $('baiduKeyGroup').style.display = '';
    $('tavilyKeyGroup').style.display = 'none';
    $('btnBaiduConsole').style.display = '';
    $('btnTavilyConsole').style.display = 'none';
  }
}

// ==============================
// Steam 库存评级饼图
// ==============================

const _steamColors = {
  'SSS': '#FFD036', 'SS': '#A78BFA', 'S': '#38BDF8',
  'A': '#4ADE80', 'B': '#A8A29E', 'C': '#FDBA74', 'D': '#78716C',
};
let _pieAnimFrame = null;

function renderSteamPie(ratings) {
  const canvas = $('steamRatingChart');
  if (!canvas) return;
  const section = $('steamRatingSection');
  const labels = ['SSS', 'SS', 'S', 'A', 'B', 'C', 'D'];
  const values = labels.map(l => (ratings && ratings[l]) ? ratings[l] : 0);
  const total = values.reduce((a, b) => a + b, 0);

  // 数据未变化时跳过 Canvas 动画重绘，避免闪烁
  const cacheKey = JSON.stringify(values);
  if (renderSteamPie._lastKey === cacheKey) return;
  renderSteamPie._lastKey = cacheKey;

  if (total === 0) {
    // 无数据时清空画布并显示空状态提示
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = '#4b4b55';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.font = '12px ' + getComputedStyle(document.body).fontFamily;
    ctx.fillText('暂无评级数据', canvas.width / 2, canvas.height / 2);
    const listEl = $('steamRatingList');
    if (listEl) listEl.innerHTML = '<div class="model-item"><span class="model-label">暂无评级数据</span></div>';
    return;
  }

  // 文字描述列表
  const listEl = $('steamRatingList');
  if (listEl) {
    let html = '';
    labels.forEach((l, i) => {
      if (values[i] > 0) {
        const pct = ((values[i] / total) * 100).toFixed(1);
        html += `<div class="model-item">
          <span class="model-label" style="color:${_steamColors[l]}">${l}</span>
          <span class="model-name">${values[i]}次</span>
          <span class="model-tokens">${pct}%</span>
        </div>`;
      }
    });
    listEl.innerHTML = html;
  }

  const ctx = canvas.getContext('2d');
  const w = canvas.width, h = canvas.height;
  const cx = w / 2, cy = h / 2;
  const radius = Math.min(cx, cy) - 10;

  const targetAngles = [];
  const targetRatios = [];
  labels.forEach((l, i) => {
    targetRatios.push(total > 0 ? values[i] / total : 0);
    targetAngles.push(total > 0 ? (values[i] / total) * Math.PI * 2 : 0);
  });

  if (_pieAnimFrame) cancelAnimationFrame(_pieAnimFrame);
  const animStart = performance.now();
  const animDuration = 800;

  function drawFrame(now) {
    const progress = Math.min(1, (now - animStart) / animDuration);
    const eased = 1 - Math.pow(1 - progress, 3); // ease-out cubic
    ctx.clearRect(0, 0, w, h);

    // 阴影
    ctx.save();
    ctx.shadowColor = 'rgba(0,0,0,0.4)';
    ctx.shadowBlur = 12;

    let startAngle = -Math.PI / 2;
    for (let i = 0; i < labels.length; i++) {
      if (values[i] === 0) continue;
      const sweep = targetAngles[i] * eased;
      const endAngle = startAngle + sweep;

      ctx.beginPath();
      ctx.moveTo(cx, cy);
      ctx.arc(cx, cy, radius, startAngle, endAngle);
      ctx.closePath();
      ctx.fillStyle = _steamColors[labels[i]];
      ctx.fill();
      startAngle = endAngle;
    }
    ctx.restore();

    // 中心白色圆（甜甜圈效果）
    ctx.beginPath();
    ctx.arc(cx, cy, radius * 0.48, 0, Math.PI * 2);
    ctx.fillStyle = '#24283b';
    ctx.fill();

    // 中心文字
    ctx.fillStyle = '#a9b1d6';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.font = 'bold 13px Inter, sans-serif';
    ctx.fillText(`共${total}次`, cx, cy);

    if (progress < 1) {
      _pieAnimFrame = requestAnimationFrame(drawFrame);
    }
  }
  _pieAnimFrame = requestAnimationFrame(drawFrame);

  // 鼠标悬浮提示
  canvas.onmousemove = function (e) {
    const rect = canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    let angle = Math.atan2(my - cy, mx - cx);
    if (angle < -Math.PI / 2) angle += Math.PI * 2;
    let startAngle = -Math.PI / 2;
    let tip = '';
    for (let i = 0; i < labels.length; i++) {
      if (values[i] === 0) continue;
      const sweep = targetAngles[i];
      const endAngle = startAngle + sweep;
      if (angle >= startAngle && angle < endAngle) {
        const pct = ((values[i] / total) * 100).toFixed(1);
        tip = `${labels[i]}: ${values[i]}次 (${pct}%)`;
        break;
      }
      startAngle = endAngle;
    }
    canvas.title = tip;
  };
}

// ==============================
// 仪表盘
// ==============================

function switchLeftMode(mode) {
  const configView = $('configView');
  const dashboardView = $('dashboardView');
  const btnCfg = $('btnConfigView');
  const btnDb = $('btnDashboardView');

  if (mode === 'dashboard') {
    configView.style.display = 'none';
    dashboardView.style.display = '';
    if (btnDb) btnDb.classList.add('active');
    if (btnCfg) btnCfg.classList.remove('active');
    refreshDashboard();
  } else {
    configView.style.display = '';
    dashboardView.style.display = 'none';
    if (btnCfg) btnCfg.classList.add('active');
    if (btnDb) btnDb.classList.remove('active');
  }
}

async function refreshDashboard() {
  try {
    const resp = await api('GET', '/api/stats');
    if (!resp.ok || !resp.data) return;

    const d = resp.data;
    const models = d.models || {};
    const modelStats = d.model_stats || {};

    // 运行时间
    if (d.uptime_seconds !== undefined) {
      const s = d.uptime_seconds;
      const h = Math.floor(s / 3600);
      const m = Math.floor((s % 3600) / 60);
      $('statUptime').textContent = h > 0 ? `${h} 小时 ${m} 分钟` : `${m} 分钟`;
    }

    // 更新数值卡片
    updateStatValue('statTrigger', d.trigger_count || 0);
    updateStatValue('statSuccess', d.reply_success || 0);
    updateStatValue('statFail', d.reply_fail || 0);
    updateStatValue('statSearch', d.search_count || 0);
    updateStatValue('statTotalTokens', d.total_tokens || 0);

    // 同步更新侧边栏快速统计
    updateStatValue('qsTrigger', d.trigger_count || 0);
    updateStatValue('qsSuccess', d.reply_success || 0);
    updateStatValue('qsFail', d.reply_fail || 0);
    updateStatValue('qsSearch', d.search_count || 0);

    // 构建模型统计列表：优先显示有调用记录的模型，再补全当前配置模型
    const listEl = $('modelStatsList');
    let html = '';
    const seenLabels = new Set();

    // 1) 先渲染 model_stats 中真实有数据的项
    Object.keys(modelStats).forEach(label => {
      const stats = modelStats[label];
      if (!stats || (!stats.tokens && !stats.calls)) return;
      seenLabels.add(label);
      const modelName = stats.model || models[label] || '未知模型';
      html += `
        <div class="model-item">
          <span class="model-label">${escapeHtml(String(label))}</span>
          <span class="model-name">${escapeHtml(String(modelName))}</span>
          <span class="model-tokens">${formatNumber(stats.tokens)} tokens · ${escapeHtml(String(stats.calls || 0))}次</span>
        </div>`;
    });

    // 2) 再补全当前配置中已设置但未产生调用的模型
    const configLabels = ['回复模型', '搜索模型', '判断模型', '联网搜索', 'AI评价库存'];
    configLabels.forEach(label => {
      if (seenLabels.has(label)) return;
      const modelName = models[label];
      if (!modelName || modelName === '未配置') return;
      html += `
        <div class="model-item">
          <span class="model-label">${escapeHtml(String(label))}</span>
          <span class="model-name">${escapeHtml(String(modelName))}</span>
          <span class="model-tokens">0 tokens · 0次</span>
        </div>`;
    });

    // 3) 兜底提示
    if (!html) {
      html = '<div class="model-item"><span class="model-label">暂无模型调用记录</span></div>';
    }
    listEl.innerHTML = html;

    // Steam 评级饼图：Steam 评价开启时始终显示区块
    const s = $('steamRatingSection');
    if (d.steam_enabled) {
      if (s) s.style.display = 'block';
      if (d.steam_ratings) {
        renderSteamPie(d.steam_ratings);
      }
    } else {
      if (s) s.style.display = 'none';
    }
  } catch (e) {
    // 静默失败
  }
}

// 数字滚动动画引擎
const _anims = {};

function animateTo(element, current, target, suffix = '') {
  const start = current;
  const end = target;
  if (start === end) return;

  const duration = Math.min(800, Math.max(300, Math.abs(end - start) * 10));
  const startTime = performance.now();

  cancelAnimationFrame(_anims[element.id]);

  function frame(now) {
    const elapsed = now - startTime;
    const progress = Math.min(1, elapsed / duration);
    // easeOutQuad
    const eased = 1 - (1 - progress) * (1 - progress);
    const current = Math.round(start + (end - start) * eased);
    element.textContent = current + suffix;
    element.classList.add('updated');

    if (progress < 1) {
      _anims[element.id] = requestAnimationFrame(frame);
    } else {
      element.classList.remove('updated');
    }
  }

  _anims[element.id] = requestAnimationFrame(frame);
}

function updateStatValue(id, value) {
  const el = $(id);
  if (!el) return;
  const newVal = typeof value === 'number' ? value : parseInt(value, 10) || 0;
  const currentVal = parseInt(el.textContent.replace(/[^0-9]/g, ''), 10) || 0;
  if (currentVal !== newVal) {
    animateTo(el, currentVal, newVal);
  }
}

function formatNumber(n) {
  if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
  if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
  return String(Math.round(n));
}

// ==============================
// 对话任务面板（日志右侧，可折叠）
// ==============================

const TASK_POLL_MS = 3000;

function initTaskPanel() {
  const panel = $('taskPanel');
  if (!panel) return;
  // 折叠状态持久化
  panel.classList.toggle('collapsed', localStorage.getItem('taskPanelCollapsed') === '1');
  updateTaskPanelToggleIcon();
  loadTasks(true);
  setInterval(() => loadTasks(false), TASK_POLL_MS);
}

function toggleTaskPanel() {
  const panel = $('taskPanel');
  if (!panel) return;
  const collapsed = !panel.classList.contains('collapsed');
  panel.classList.toggle('collapsed', collapsed);
  localStorage.setItem('taskPanelCollapsed', collapsed ? '1' : '0');
  updateTaskPanelToggleIcon();
  if (!collapsed) loadTasks(true);
}

function updateTaskPanelToggleIcon() {
  const panel = $('taskPanel');
  const btn = $('taskPanelToggle');
  if (!panel || !btn) return;
  const collapsed = panel.classList.contains('collapsed');
  btn.textContent = collapsed ? '◀' : '▶';
  btn.title = collapsed ? '展开任务面板' : '折叠任务面板';
}

function resetTaskFilters() {
  $('taskFilterKeyword').value = '';
  $('taskFilterUid').value = '';
  $('taskFilterDate').value = '';
  $('taskFilterStatus').value = '';
  loadTasks(true);
}

async function loadTasks(manual) {
  const listEl = $('taskList');
  if (!listEl) return;
  // 折叠时跳过自动轮询（手动展开/搜索才拉取，省请求）
  if (!manual && $('taskPanel').classList.contains('collapsed')) return;
  try {
    const params = new URLSearchParams();
    const kw = $('taskFilterKeyword').value.trim();
    const uid = $('taskFilterUid').value.trim();
    const date = $('taskFilterDate').value;
    const status = $('taskFilterStatus').value;
    if (kw) params.set('keyword', kw);
    if (uid) params.set('uid', uid);
    if (date) params.set('date', date);
    if (status) params.set('status', status);
    const resp = await api('GET', '/api/tasks?' + params.toString());
    if (!resp.ok) return;
    // 首次填充状态下拉
    const select = $('taskFilterStatus');
    if (select && select.options.length <= 1 && resp.status_labels) {
      Object.entries(resp.status_labels).forEach(([key, label]) => {
        const opt = document.createElement('option');
        opt.value = key;
        opt.textContent = label;
        select.appendChild(opt);
      });
    }
    renderTasks(resp.data || []);
  } catch (e) {
    // 静默失败，下个轮询周期重试
  }
}

function fmtTaskTime(ts) {
  const d = new Date(ts * 1000);
  const p = n => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function renderTasks(tasks) {
  const listEl = $('taskList');
  $('taskPanelCount').textContent = String(tasks.length);
  if (!tasks.length) {
    listEl.innerHTML = '<div class="task-empty">暂无匹配的任务</div>';
    return;
  }
  let html = '';
  tasks.forEach(t => {
    const avatarHtml = isSafeAvatarUrl(t.avatar)
      ? `<img src="${escapeHtml(t.avatar)}" class="task-avatar" onerror="this.outerHTML='<span class=\\'task-avatar\\'></span>'">`
      : `<span class="task-avatar"></span>`;
    let blocks = `
        <div class="task-block">
          <div class="task-block-label">提问</div>
          <div class="task-block-text">${escapeHtml(t.question || '（无附加文字）')}</div>
        </div>`;
    if (t.reply_text) {
      blocks += `
        <div class="task-block task-reply">
          <div class="task-block-label">AI 回复</div>
          <div class="task-block-text">${escapeHtml(t.reply_text)}</div>
        </div>`;
    }
    if (t.error && (t.status === 'failed' || t.status === 'skipped')) {
      blocks += `
        <div class="task-block ${t.status === 'failed' ? 'task-error' : ''}">
          <div class="task-block-label">${t.status === 'failed' ? '失败原因' : '跳过原因'}</div>
          <div class="task-block-text">${escapeHtml(t.error)}</div>
        </div>`;
    }
    const timeRange = (t.updated_at > t.created_at + 1)
      ? `${fmtTaskTime(t.created_at)} → ${fmtTaskTime(t.updated_at)}`
      : fmtTaskTime(t.created_at);
    html += `
      <div class="task-card">
        <div class="task-card-header">
          ${avatarHtml}
          <div class="task-user"><b>${escapeHtml(t.username || '未知用户')}</b> <span class="task-uid">UID:${escapeHtml(t.user_id || '-')}</span></div>
          <span class="task-status st-${escapeHtml(t.status)}">${escapeHtml(t.status_label || t.status)}</span>
        </div>
        ${blocks}
        <div class="task-time">${timeRange}</div>
      </div>`;
  });
  listEl.innerHTML = html;
}

// ==============================
// 标签页切换
// ==============================

function initTabs() {
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', function () {
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      this.classList.add('active');

      const tabId = this.getAttribute('data-tab');
      document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
      const tab = $(tabId);
      if (tab) tab.classList.add('active');
    });
  });
}

// ==============================
// 初始化
// ==============================

document.addEventListener('DOMContentLoaded', async function () {
  initTabs();
  initTaskPanel();

  // 控制中心用户区点击：未登录则登录，已登录则退出当前账号以便切换
  const missionUser = $('missionUser');
  if (missionUser) {
    missionUser.addEventListener('click', () => {
      if (!STATE.loggedIn) {
        showLoginModal();
      } else {
        handleLogout();
      }
    });
  }

  // 清空日志区域，等待 SSE 推送
  $('logContainer').innerHTML = `
    <div class="log-entry log-info">
      <span class="log-prompt">$</span>
      <span class="log-time">--:--:--</span>
      <span class="log-level">[INFO]</span>
      <span class="log-msg">正在连接日志服务...</span>
      <span class="log-cursor"></span>
    </div>
  `;

  // 启动 SSE 日志流（SSE 会自动推送最近历史日志，包括启动自检）
  initLogStream();

  // 立即刷新机器人状态，确保页面加载后显示真实状态
  await refreshBotStatus();

  // 加载配置
  await loadConfig();

  // 检测登录状态并显示结果
  await checkLoginStatus();

  // 启动本地实时倒计时
  startCountdownTimer();

  // 启动仪表盘定时刷新（当仪表盘视图可见时）
  setInterval(() => {
    const dv = $('dashboardView');
    if (dv && dv.style.display !== 'none') {
      refreshDashboard();
    }
  }, DASHBOARD_REFRESH_MS);

  // 定期刷新状态
  setInterval(refreshBotStatus, STATUS_REFRESH_MS);
});