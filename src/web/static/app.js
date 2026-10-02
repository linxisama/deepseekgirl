/* 双端（微信 / QQ）仪表盘前端逻辑 */

const API = '/api';
let ws = null;
let reconnectTimer = null;
let statusTimer = null;
let heartbeatTimer = null;
let botActionPending = false;
let currentPlatform = 'wechat';

const MAX_FEED_ITEMS = 200;

// === 平台页切换 ===
function switchPlatform(platform) {
    let name;
    if (platform === 'qq') {
        name = 'qq';
    } else if (platform === 'wecom') {
        name = 'wecom';
    } else {
        name = 'wechat';
    }
    currentPlatform = name;

    document.querySelectorAll('.platform-tab').forEach((tab) => {
        tab.classList.toggle('active', tab.dataset.platform === name);
    });
    document.querySelectorAll('.platform-page').forEach((page) => {
        page.classList.toggle('active', page.id === `page-${name}`);
    });

    // 切换后刷新该平台数据
    if (name === 'qq') {
        loadQQConfig();
        loadQQRooms();
    } else if (name === 'wecom') {
        loadWecomConfig();
    }
    loadGroups(name);
    loadStatus();
}

function initTabs() {
    document.querySelectorAll('.platform-tab').forEach((tab) => {
        tab.addEventListener('click', () => switchPlatform(tab.dataset.platform));
    });
}

// === WebSocket ===
function connectWS() {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
        return;
    }

    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(`${protocol}//${location.host}/ws`);

    ws.onopen = () => setWSStatus('connected');
    ws.onclose = () => {
        ws = null;
        setWSStatus('disconnected');
        scheduleReconnect();
    };
    ws.onerror = () => setWSStatus('disconnected');

    ws.onmessage = (e) => {
        try {
            const msg = JSON.parse(e.data);
            handleEvent(msg.event, msg.data, msg.time);
        } catch (err) {
            console.error('Parse error:', err);
        }
    };
}

function scheduleReconnect() {
    if (reconnectTimer) return;
    reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        connectWS();
    }, 3000);
}

function disconnect() {
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    if (statusTimer) { clearInterval(statusTimer); statusTimer = null; }
    if (heartbeatTimer) { clearInterval(heartbeatTimer); heartbeatTimer = null; }

    if (ws) {
        ws.onclose = null;
        ws.onerror = null;
        try { ws.close(1000); } catch (e) {}
        ws = null;
    }
}

function setWSStatus(status) {
    const dot = document.getElementById('ws-status');
    if (dot) dot.className = 'status-dot ' + status;
}

// === 事件处理（按平台分流，避免双端串台）===
function handleEvent(event, data, time) {
    const payload = data || {};
    const platform = payload.platform === 'qq' ? 'qq' : 'wechat';
    const timeStr = time ? new Date(time).toLocaleTimeString() : '';

    switch (event) {
        case 'message_received':
            addFeedItem(platform, 'in', timeStr, `[${payload.room}] ${payload.sender}: ${payload.content}`);
            break;
        case 'message_replied':
            addFeedItem(platform, 'out', timeStr, `[${payload.room}] 回复: ${payload.reply}`);
            break;
        case 'sticker_sent':
            addFeedItem(platform, 'out', timeStr, `[${payload.room}] 收藏表情`);
            break;
        case 'sticker_failed':
            addFeedItem(platform, 'event', timeStr, `[${payload.room}] 收藏表情发送失败`);
            break;
        case 'message_blocked':
            addFeedItem(platform, 'event', timeStr, `消息被安全过滤: ${payload.reason}`);
            break;
        case 'message_throttled':
            addFeedItem(platform, 'event', timeStr, '消息被限流');
            break;
        case 'ai_error':
            addFeedItem(platform, 'event', timeStr, `AI 错误: ${payload.error}`);
            break;
        case 'queue_full':
            addFeedItem(platform, 'event', timeStr, '队列已满，消息被丢弃');
            break;
        case 'bot_running':
            addFeedItem(platform, 'event', timeStr, '机器人已启动');
            updateTabState(platform, true, '运行中');
            break;
        case 'bot_stopped':
            addFeedItem(platform, 'event', timeStr, '机器人已停止');
            updateTabState(platform, false, '已停止');
            break;
        case 'bot_start_failed':
            addFeedItem(platform, 'event', timeStr, `启动失败: ${payload.message || ''}`);
            updateTabState(platform, false, '启动失败');
            loadStatus();
            break;
        case 'bot_starting':
            updateTabState(platform, false, '启动中');
            break;
        case 'status_update':
            updateStats(payload);
            break;
        case 'pong':
        case 'heartbeat':
            break;
        default:
            addFeedItem(platform, 'event', timeStr, `${event}: ${JSON.stringify(payload)}`);
    }
}

function addFeedItem(platform, type, time, text) {
    const feed = document.getElementById(`feed-${platform}`);
    if (!feed) return;

    const div = document.createElement('div');
    div.className = 'feed-item';
    div.innerHTML = `<span class="feed-time">${time}</span><span class="feed-${type}">${escapeHtml(text)}</span>`;
    feed.appendChild(div);

    while (feed.children.length > MAX_FEED_ITEMS) {
        feed.removeChild(feed.firstChild);
    }
    feed.scrollTop = feed.scrollHeight;
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str == null ? '' : String(str);
    return div.innerHTML;
}

// === API ===
async function fetchJSON(url, options = {}) {
    try {
        const res = await fetch(API + url, {
            headers: { 'Content-Type': 'application/json' },
            ...options,
        });
        return await res.json();
    } catch (err) {
        console.error('API error:', err);
        return null;
    }
}

async function loadStatus() {
    const data = await fetchJSON('/status');
    if (!data) return;
    updateStats(data);
    updateVoiceStatus(data.voice);
}

function setText(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
}

function setValue(id, value) {
    const el = document.getElementById(id);
    if (el) el.value = value === undefined || value === null ? '' : value;
}

function setChecked(id, value) {
    const el = document.getElementById(id);
    if (el) el.checked = Boolean(value);
}

function updateTabState(platform, running, textOverride) {
    const dot = document.getElementById(`tab-dot-${platform}`);
    const state = document.getElementById(`tab-state-${platform}`);
    if (dot) dot.className = 'tab-dot ' + (running ? 'on' : 'off');
    if (state) state.textContent = textOverride || (running ? '运行中' : '未启动');
}

function statusText(status) {
    return {
        connected: '已连接',
        connecting: '连接中',
        disconnected: '未连接',
        error: '异常',
        unknown: '未知',
    }[status] || (status || '未知');
}

function updateStats(data) {
    const platforms = data.platforms || {};
    const wx = platforms.wechat || {};
    const qq = platforms.qq || {};
    const wecom = platforms.wecom || {};

    // --- 微信页 ---
    const wxBridge = wx.bridge || data.wechat || {};
    setText('stat-wx-conn', statusText(wxBridge.status));
    updateTabState('wechat', Boolean(wx.running), wx.running ? '运行中' : '未启动');

    // --- QQ 页 ---
    const qqBridge = qq.bridge || data.qq || {};
    const qqEnabled = qq.enabled !== undefined ? qq.enabled : data.qq_enabled;
    setText('stat-qq-conn', qqEnabled ? statusText(qqBridge.status) : '未启用');
    setText('stat-qq-run', qq.running ? '运行中' : '未启动');
    setText('stat-qq-rooms', String(qqBridge.rooms || 0));
    if (qq.handler) setText('stat-qq-queue', String(qq.handler.queue_size || 0));
    updateTabState(
        'qq',
        Boolean(qq.running),
        !qqEnabled ? '未启用' : (qq.running ? '运行中' : '未启动')
    );

    // --- 企业微信页 ---
    const wecomBridge = wecom.bridge || data.wecom || {};
    const wecomEnabled = wecom.enabled !== undefined ? wecom.enabled : data.wecom_enabled;
    setText('stat-wecom-conn', wecomEnabled ? statusText(wecomBridge.status) : '未启用');
    setText('stat-wecom-run', wecom.running ? '运行中' : '未启动');
    setText('stat-wecom-rooms', String(wecomBridge.rooms || 0));
    if (wecom.handler) setText('stat-wecom-queue', String(wecom.handler.queue_size || 0));
    updateTabState(
        'wecom',
        Boolean(wecom.running),
        !wecomEnabled ? '未启用' : (wecom.running ? '运行中' : '未启动')
    );

    // 企业微信连接异常提示
    const wecomNotice = document.getElementById('wecom-result');
    if (wecomNotice && wecomBridge.last_error) {
        wecomNotice.textContent = `连接异常: ${wecomBridge.last_error}`;
    } else if (wecomNotice && wecomEnabled && !wecomBridge.configured) {
        wecomNotice.textContent = wecomBridge.unavailable || '尚未配置 Bot ID / Secret';
    }

    // QQ 连接异常时提示原因
    const qqNotice = document.getElementById('qq-result');
    if (qqNotice && qqBridge.last_error) {
        qqNotice.textContent = `连接异常: ${qqBridge.last_error}`;
    } else if (qqNotice && qqEnabled && !qqBridge.configured) {
        qqNotice.textContent = qqBridge.unavailable || '尚未配置 AppID / AppSecret';
    }

    // --- 共享状态 ---
    if (data.ai) {
        setText('stat-ai', data.ai.available ? '可用' : '不可用');
        if (data.ai.usage) {
            setText('stat-tokens', (data.ai.usage.total_tokens || 0).toLocaleString());
        }
    }
    setText('stat-style', data.style_display || data.style || 'default');

    // 安全状态（各端独立）
    if (wx.security) {
        setText('sec-circuit', wx.security.circuit_state || '--');
        setText('sec-tokens', wx.security.available_tokens ?? '--');
    } else if (data.security) {
        setText('sec-circuit', data.security.circuit_state || '--');
        setText('sec-tokens', data.security.available_tokens ?? '--');
    }
    if (qq.security) {
        setText('qq-sec-circuit', qq.security.circuit_state || '--');
        setText('qq-sec-tokens', qq.security.available_tokens ?? '--');
    }

    // 启停按钮状态
    if (!botActionPending) {
        const wxRunning = Boolean(wx.running);
        const startWx = document.getElementById('btn-start-wx');
        const stopWx = document.getElementById('btn-stop-wx');
        if (startWx) startWx.disabled = wxRunning;
        if (stopWx) stopWx.disabled = !wxRunning;

        const qqRunning = Boolean(qq.running);
        const startQQ = document.getElementById('btn-start-qq');
        const stopQQ = document.getElementById('btn-stop-qq');
        if (startQQ) startQQ.disabled = qqRunning || !qqEnabled;
        if (stopQQ) stopQQ.disabled = !qqRunning;
    }
}

// === 风格 ===
async function loadStyles() {
    const data = await fetchJSON('/styles');
    if (!data) return;

    const select = document.getElementById('style-select');
    if (!select) return;
    select.innerHTML = '';
    (data.styles || []).forEach((s) => {
        const opt = document.createElement('option');
        opt.value = s.name;
        opt.textContent = `${s.display_name} - ${s.description}`;
        if (s.active) opt.selected = true;
        select.appendChild(opt);
    });

    const locked = Boolean(data.locked);
    select.disabled = locked;
    const applyBtn = document.getElementById('btn-apply-style');
    if (applyBtn) applyBtn.disabled = locked;
    const note = document.getElementById('style-lock-note');
    if (note) {
        note.hidden = !locked;
        note.textContent = locked
            ? `${data.locked_display_name || data.locked_name || '固定人设'} 已永久锁定，不能更改`
            : '';
    }
}

// === AI 配置 ===
async function loadAIConfig() {
    const data = await fetchJSON('/config');
    if (!data || data.error) return;
    const url = document.getElementById('config-api-url');
    const key = document.getElementById('config-api-key');
    const model = document.getElementById('config-model');
    const style = document.getElementById('config-api-style');
    const result = document.getElementById('config-result');
    if (url) url.value = data.api_url || '';
    if (model) model.value = data.model || '';
    if (style) style.value = data.api_style || 'auto';
    if (key) {
        key.value = '';
        key.placeholder = data.api_key_set ? '已保存，留空则不修改' : '请输入 API Key';
    }
    if (result) {
        result.textContent = data.api_key_set
            ? '配置已加载，API Key 已安全保存'
            : '配置已加载，尚未设置 API Key';
    }
}

// === 群聊监控（按平台）===
function renderGroups(listId, groups, platform, emptyText) {
    const list = document.getElementById(listId);
    if (!list) return;
    list.innerHTML = '';

    if (!groups.length) {
        const empty = document.createElement('div');
        empty.className = 'group-empty';
        empty.textContent = emptyText;
        list.appendChild(empty);
        return;
    }

    groups.forEach((name) => {
        const row = document.createElement('div');
        row.className = 'group-row';

        const label = document.createElement('div');
        label.className = 'group-name';
        label.textContent = name;

        const remove = document.createElement('button');
        remove.type = 'button';
        remove.className = 'btn btn-red group-remove';
        remove.textContent = '删除';
        remove.title = `停止监控 ${name}`;
        remove.addEventListener('click', () => removeGroup(name, platform));

        row.appendChild(label);
        row.appendChild(remove);
        list.appendChild(row);
    });
}

async function loadGroups(platform = currentPlatform) {
    const data = await fetchJSON(`/groups?platform=${platform}`);
    const resultId = platform === 'qq' ? 'qq-group-result' : 'group-result';
    const listId = platform === 'qq' ? 'qq-group-list' : 'group-list';
    const result = document.getElementById(resultId);

    if (!data || data.error) {
        if (result) result.textContent = (data && data.error) || '监控列表读取失败';
        return;
    }

    const groups = Array.isArray(data.groups) ? data.groups : [];
    renderGroups(
        listId,
        groups,
        platform,
        platform === 'qq' ? '未指定群，将回应所有会话' : '尚未添加监控群聊'
    );

    if (result) {
        if (platform === 'qq') {
            const rooms = Array.isArray(data.rooms) ? data.rooms.length : 0;
            result.textContent = data.listening
                ? `QQ 端运行中，已发现 ${rooms} 个会话`
                : `已配置 ${groups.length} 条监控规则${groups.length ? '' : '（=回应全部）'}，启动后生效`;
        } else {
            result.textContent = data.listening
                ? `正在监控 ${data.active_group_count} 个群聊`
                : `已配置 ${groups.length} 个群聊，机器人启动后生效`;
        }
    }
}

async function addGroup(platform) {
    const inputId = platform === 'qq' ? 'qq-group-input' : 'group-name-input';
    const resultId = platform === 'qq' ? 'qq-group-result' : 'group-result';
    const btnId = platform === 'qq' ? 'btn-add-group-qq' : 'btn-add-group';
    const input = document.getElementById(inputId);
    const result = document.getElementById(resultId);
    const addBtn = document.getElementById(btnId);
    const name = input ? input.value.trim() : '';
    if (!name) {
        if (result) result.textContent = platform === 'qq' ? '请输入群 openid' : '请输入群聊名称';
        if (input) input.focus();
        return;
    }

    if (addBtn) addBtn.disabled = true;
    if (result) result.textContent = '正在添加...';
    const data = await fetchJSON('/groups', {
        method: 'POST',
        body: JSON.stringify({ name, platform }),
    });
    if (addBtn) addBtn.disabled = false;

    if (data && data.success) {
        if (input) input.value = '';
        await loadGroups(platform);
        if (result) result.textContent = data.message || '已添加';
    } else if (result) {
        result.textContent = (data && data.message) || '添加失败';
    }
}

async function removeGroup(name, platform) {
    const resultId = platform === 'qq' ? 'qq-group-result' : 'group-result';
    const result = document.getElementById(resultId);
    if (result) result.textContent = `正在删除 ${name}...`;
    const data = await fetchJSON('/groups/remove', {
        method: 'POST',
        body: JSON.stringify({ name, platform }),
    });
    if (data && data.success) {
        await loadGroups(platform);
        if (result) result.textContent = data.message || '已删除';
    } else if (result) {
        result.textContent = (data && data.message) || '删除失败';
    }
}

// === QQ 配置 ===
async function loadQQConfig() {
    const data = await fetchJSON('/qq/config');
    if (!data || data.error) return;

    const appId = document.getElementById('qq-app-id');
    const secret = document.getElementById('qq-secret');
    const sandbox = document.getElementById('qq-sandbox');
    const backend = document.getElementById('qq-backend');
    const enabled = document.getElementById('qq-enabled');
    const result = document.getElementById('qq-config-result');

    if (appId) appId.value = data.app_id || '';
    if (secret) {
        secret.value = '';
        secret.placeholder = data.secret_set ? '已保存，留空则不修改' : '请输入 AppSecret';
    }
    if (sandbox) sandbox.checked = Boolean(data.sandbox);
    if (backend) backend.value = data.backend || 'official';
    if (enabled) enabled.checked = Boolean(data.enabled);

    if (result) {
        const status = (data.status && data.status.status) || 'disconnected';
        result.textContent = data.enabled
            ? `QQ 端已启用，连接状态: ${statusText(status)}`
            : 'QQ 端尚未启用，填写凭证后勾选「启用 QQ 端」并保存';
    }
}

async function saveQQConfig() {
    const result = document.getElementById('qq-config-result');
    const btn = document.getElementById('btn-save-qq');
    const payload = {
        app_id: (document.getElementById('qq-app-id') || {}).value || '',
        client_secret: (document.getElementById('qq-secret') || {}).value || '',
        sandbox: Boolean((document.getElementById('qq-sandbox') || {}).checked),
        backend: (document.getElementById('qq-backend') || {}).value || 'official',
        enabled: Boolean((document.getElementById('qq-enabled') || {}).checked),
    };

    if (btn) btn.disabled = true;
    if (result) result.textContent = '正在保存并应用...';
    const data = await fetchJSON('/qq/config', {
        method: 'POST',
        body: JSON.stringify(payload),
    });
    if (btn) btn.disabled = false;

    if (data && data.success) {
        if (result) result.textContent = data.message || '已保存';
        const secret = document.getElementById('qq-secret');
        if (secret) {
            secret.value = '';
            secret.placeholder = '已保存，留空则不修改';
        }
        await loadStatus();
        await loadGroups('qq');
    } else if (result) {
        result.textContent = (data && data.message) || '保存失败';
    }
}

// === 企业微信配置 ===
async function loadWecomConfig() {
    const data = await fetchJSON('/status');
    if (!data) return;

    const botId = document.getElementById('wecom-bot-id');
    const secret = document.getElementById('wecom-secret');
    const corpId = document.getElementById('wecom-corp-id');
    const agentId = document.getElementById('wecom-agent-id');
    const enabled = document.getElementById('wecom-enabled');
    const result = document.getElementById('wecom-config-result');

    const wecom = data.wecom || {};

    if (botId) botId.value = wecom.bot_id || '';
    if (secret) {
        secret.value = '';
        secret.placeholder = wecom.secret_set ? '已保存，留空则不修改' : '请输入 Secret';
    }
    if (corpId) corpId.value = wecom.corp_id || '';
    if (agentId) agentId.value = wecom.agent_id || '';
    if (enabled) enabled.checked = Boolean(data.wecom_enabled);

    if (result) {
        const status = (wecom.status && wecom.status.status) || 'disconnected';
        result.textContent = data.wecom_enabled
            ? `企业微信端已启用，连接状态: ${statusText(status)}`
            : '企业微信端尚未启用，填写凭证后勾选「启用企业微信端」并保存';
    }
}

async function saveWecomConfig() {
    const result = document.getElementById('wecom-config-result');
    const btn = document.getElementById('btn-save-wecom');
    const payload = {
        bot_id: (document.getElementById('wecom-bot-id') || {}).value || '',
        secret: (document.getElementById('wecom-secret') || {}).value || '',
        corp_id: (document.getElementById('wecom-corp-id') || {}).value || '',
        agent_id: (document.getElementById('wecom-agent-id') || {}).value || '',
        enabled: Boolean((document.getElementById('wecom-enabled') || {}).checked),
    };

    if (btn) btn.disabled = true;
    if (result) result.textContent = '正在保存并应用...';

    // 通过更新配置API保存
    const data = await fetchJSON('/config', {
        method: 'POST',
        body: JSON.stringify({
            wecom: payload,
        }),
    });

    if (btn) btn.disabled = false;

    if (data && data.success) {
        if (result) result.textContent = data.message || '已保存';
        const secret = document.getElementById('wecom-secret');
        if (secret) {
            secret.value = '';
            secret.placeholder = '已保存，留空则不修改';
        }
        await loadStatus();
    } else if (result) {
        result.textContent = (data && data.message) || '保存失败';
    }
}

async function loadQQRooms() {
    const data = await fetchJSON('/qq/rooms');
    const box = document.getElementById('qq-rooms');
    if (!box) return;
    box.innerHTML = '';
    const rooms = (data && Array.isArray(data.rooms)) ? data.rooms : [];
    if (!rooms.length) {
        const empty = document.createElement('div');
        empty.className = 'group-empty';
        empty.textContent = '尚未收到 QQ 消息';
        box.appendChild(empty);
        return;
    }
    rooms.forEach((room) => {
        const row = document.createElement('div');
        row.className = 'group-row';
        const label = document.createElement('div');
        label.className = 'group-name';
        label.textContent = `${room.display}（${room.kind}）`;

        label.title = room.openid;
        row.appendChild(label);
        box.appendChild(row);
    });
}

// ===========================================================================
// 语音 / 点歌
// ===========================================================================
function voiceSummary(status) {
    if (!status) return '语音 / 点歌：状态未知';
    if (!status.enabled) return '语音 / 点歌：已关闭';
    const tts = status.tts || {};
    const song = status.song || {};
    const asr = status.asr || {};
    const parts = [
        `引擎 ${tts.engine_label || tts.engine || '--'}`,
        `语音识别 ${asr.loaded ? '已就绪' : (asr.enabled ? '加载中' : '关闭')}`,
        `曲库 ${song.song_count || 0} 首`,
        `已缓存 ${song.cached_count || 0} 首`,
        `缓存命中 ${tts.cache_hits || 0} 次`,
        `平均合成 ${tts.avg_seconds || 0}s`,
    ];
    if (tts.failures) parts.push(`失败 ${tts.failures} 次`);
    return parts.join(' · ');
}

function updateVoiceStatus(voice) {
    const text = voiceSummary(voice);
    setText('voice-status', text);
    setText('qq-voice-status', text);
    if (!voice) return;
    const song = voice.song || {};
    setText('song-count', song.song_count || 0);
    setText('song-cached', song.cached_count || 0);
}

async function loadVoiceConfig() {
    const data = await fetchJSON('/voice/config');
    if (!data || data.error) return;

    setChecked('voice-enabled', data.enabled);
    setChecked('voice-quick', data.quick_replies);
    setValue('voice-name', data.default_voice);
    setValue('voice-rate', data.rate);
    setValue('voice-pitch', data.pitch);
    setValue('voice-sing-rate', data.sing_rate);
    setValue('voice-sing-pitch', data.sing_pitch);
    updateVoiceStatus(data.status);

    const result = document.getElementById('voice-result');
    if (result) {
        result.textContent = data.enabled
            ? '语音 / 点歌已启用'
            : '语音 / 点歌当前关闭';
    }
}

async function saveVoiceConfig() {
    const result = document.getElementById('voice-result');
    const payload = {
        enabled: document.getElementById('voice-enabled').checked,
        quick_replies: document.getElementById('voice-quick').checked,
        default_voice: document.getElementById('voice-name').value,
        rate: document.getElementById('voice-rate').value || '+0%',
        pitch: document.getElementById('voice-pitch').value || '+0Hz',
        sing_rate: document.getElementById('voice-sing-rate').value || '+8%',
        sing_pitch: document.getElementById('voice-sing-pitch').value || '+35Hz',
    };
    if (result) result.textContent = '正在保存...';

    const data = await fetchJSON('/voice/config', {
        method: 'POST',
        body: JSON.stringify(payload),
    });
    if (!data) {
        if (result) result.textContent = '保存失败：无法连接服务';
        return;
    }
    if (result) result.textContent = data.message || (data.success ? '已保存' : '保存失败');
    if (data.status) updateVoiceStatus(data.status);
    loadSongs();
}

function renderSongChips(songs) {
    const box = document.getElementById('song-list');
    if (!box) return;
    box.innerHTML = '';

    if (!Array.isArray(songs) || !songs.length) {
        const empty = document.createElement('div');
        empty.className = 'group-empty';
        empty.textContent = '曲库为空或语音功能未启用';
        box.appendChild(empty);
        return;
    }

    songs.forEach((song) => {
        const chip = document.createElement('span');
        chip.className = 'song-chip' + (song.cached ? ' cached' : '');
        chip.textContent = song.title;
        chip.title = `${song.artist || ''}${song.cached ? '（已缓存，点歌秒发）' : '（首次点歌需合成）'}`;
        box.appendChild(chip);
    });
}

async function loadSongs() {
    const data = await fetchJSON('/songs');
    const songs = (data && Array.isArray(data.songs)) ? data.songs : [];
    renderSongChips(songs);

    const pick = document.getElementById('qq-song-pick');
    if (pick) {
        pick.innerHTML = '';
        if (!songs.length) {
            const opt = document.createElement('option');
            opt.value = '';
            opt.textContent = '（语音功能未启用）';
            pick.appendChild(opt);
        }
        songs.forEach((song) => {
            const opt = document.createElement('option');
            opt.value = song.id;
            opt.textContent = `${song.title} · ${song.artist || ''}`;
            pick.appendChild(opt);
        });
    }
}

async function loadQqSongOptions() {
    const data = await fetchJSON('/voice/songs/qq');
    if (!data) return;

    const pick = document.getElementById('qq-song-pick');
    if (pick && Array.isArray(data.songs) && data.songs.length) {
        pick.innerHTML = '';
        data.songs.forEach((song) => {
            const opt = document.createElement('option');
            opt.value = song.id;
            opt.textContent = `${song.title} · ${song.artist || ''}`;
            pick.appendChild(opt);
        });
    }

    const roomSel = document.getElementById('qq-song-room');
    if (!roomSel) return;
    const previous = roomSel.value;
    roomSel.innerHTML = '';
    const rooms = Array.isArray(data.rooms) ? data.rooms : [];
    if (!rooms.length) {
        const opt = document.createElement('option');
        opt.value = '';
        opt.textContent = '（尚未发现会话，请先在 QQ 里给机器人发一条消息）';
        roomSel.appendChild(opt);
        return;
    }
    rooms.forEach((room) => {
        const opt = document.createElement('option');
        opt.value = room.display;
        opt.textContent = `${room.display}（${room.kind}）`;
        roomSel.appendChild(opt);
    });
    if (previous && rooms.some((room) => room.display === previous)) {
        roomSel.value = previous;
    }
}

async function previewVoice() {
    const input = document.getElementById('voice-preview-text');
    const result = document.getElementById('voice-result');
    const player = document.getElementById('voice-player');
    const text = input ? input.value.trim() : '';
    if (!text) {
        if (result) result.textContent = '请先输入要试听的内容';
        return;
    }
    if (result) result.textContent = '正在合成试听音频...';

    const data = await fetchJSON('/voice/preview', {
        method: 'POST',
        body: JSON.stringify({
            text,
            voice: document.getElementById('voice-name').value,
            singing: true,
        }),
    });
    if (!data || !data.success) {
        if (result) result.textContent = (data && data.message) || '合成失败';
        return;
    }
    if (result) result.textContent = `试听就绪（约 ${data.duration}s）`;
    if (player) {
        player.src = data.url;
        player.hidden = false;
        player.play().catch(() => {});
    }
}

async function playSongToQQ(random = false) {
    const result = document.getElementById('qq-song-result');
    const room = document.getElementById('qq-song-room').value;
    if (!room) {
        if (result) result.textContent = '请先选择一个目标会话';
        return;
    }
    const song = random ? '' : document.getElementById('qq-song-pick').value;
    if (result) result.textContent = '正在合成并发送...';

    const data = await fetchJSON('/songs/play', {
        method: 'POST',
        body: JSON.stringify({ platform: 'qq', room, song }),
    });
    if (result) {
        result.textContent = (data && data.message) || '发送失败';
    }
}

// === 音乐源 ===
function musicSummary(status) {
    if (!status) return '音乐源：状态未知';
    if (!status.enabled) return '音乐源：未启用（点歌将使用机器人哼唱）';
    const parts = [
        `服务器 ${status.base_url || '--'}`,
        status.logged_in ? '已登录' : '未登录',
        `音质 ${status.quality || '--'}`,
        `已缓存 ${status.cached_count || 0} 首 (${status.cache_size_mb || 0}MB)`,
        `下载 ${status.downloads || 0} 首`,
    ];
    if (status.failures) parts.push(`失败 ${status.failures} 次`);
    return '音乐源：' + parts.join(' · ');
}

async function loadMusicConfig() {
    const data = await fetchJSON('/music/config');
    if (!data || data.error) return;
    setChecked('music-enabled', data.enabled);
    setValue('music-base-url', data.base_url);
    setValue('music-quality', data.quality || '128k');
    const pwd = document.getElementById('music-password');
    if (pwd) {
        pwd.value = '';
        pwd.placeholder = data.password_set ? '已保存，留空则不修改' : '请输入访问密码';
    }
    setText('music-status', musicSummary(data.status));
    const result = document.getElementById('music-result');
    if (result) {
        result.textContent = data.enabled ? '音乐源已启用' : '音乐源当前关闭';
    }
}

async function saveMusicConfig() {
    const result = document.getElementById('music-result');
    const payload = {
        enabled: document.getElementById('music-enabled').checked,
        base_url: document.getElementById('music-base-url').value.trim(),
        quality: document.getElementById('music-quality').value,
        password: document.getElementById('music-password').value,
    };
    if (result) result.textContent = '正在保存...';
    const data = await fetchJSON('/music/config', {
        method: 'POST',
        body: JSON.stringify(payload),
    });
    if (!data) {
        if (result) result.textContent = '保存失败：无法连接服务';
        return;
    }
    if (result) result.textContent = data.message || (data.success ? '已保存' : '保存失败');
    loadMusicConfig();
}

async function testMusic() {
    const result = document.getElementById('music-result');
    if (result) result.textContent = '正在测试（认证 → 搜索 → 解析）...';
    const data = await fetchJSON('/music/test', {
        method: 'POST',
        body: JSON.stringify({ keyword: '青花瓷' }),
    });
    if (result) {
        result.textContent = (data && data.message) || '测试失败';
    }
    loadMusicConfig();
}

// === 启停 ===
async function startPlatform(platform) {
    let resultId;
    if (platform === 'qq') resultId = 'qq-result';
    else if (platform === 'wecom') resultId = 'wecom-result';
    else resultId = 'wx-result';
    const result = document.getElementById(resultId);
    botActionPending = true;
    if (result) {
        if (platform === 'qq') {
            result.textContent = '正在连接 QQ 开放平台...';
        } else if (platform === 'wecom') {
            result.textContent = '正在连接企业微信...';
        } else {
            result.textContent = '正在连接微信，请保持微信窗口可见...';
        }
    }
    const data = await fetchJSON('/bot/start', {
        method: 'POST',
        body: JSON.stringify({ platform }),
    });
    botActionPending = false;
    if (result) result.textContent = (data && data.message) || '启动完成';
    await loadStatus();
}

async function stopPlatform(platform) {
    let resultId;
    if (platform === 'qq') resultId = 'qq-result';
    else if (platform === 'wecom') resultId = 'wecom-result';
    else resultId = 'wx-result';
    const result = document.getElementById(resultId);
    botActionPending = true;
    if (result) result.textContent = '正在停止...';
    const data = await fetchJSON('/bot/stop', {
        method: 'POST',
        body: JSON.stringify({ platform }),
    });
    botActionPending = false;
    if (result) result.textContent = (data && data.message) || '已停止';
    await loadStatus();
}

async function startAll() {
    const result = document.getElementById('wx-result');
    botActionPending = true;
    if (result) result.textContent = '正在启动全部平台...';
    const data = await fetchJSON('/bot/start', {
        method: 'POST',
        body: JSON.stringify({ platform: '' }),
    });
    botActionPending = false;
    if (result) result.textContent = (data && data.message) || '已启动';
    await loadStatus();
}

async function stopAll() {
    const result = document.getElementById('wx-result');
    botActionPending = true;
    if (result) result.textContent = '正在停止全部平台...';
    const data = await fetchJSON('/bot/stop', {
        method: 'POST',
        body: JSON.stringify({ platform: '' }),
    });
    botActionPending = false;
    if (result) result.textContent = (data && data.message) || '已停止';
    await loadStatus();
}

async function loadHistory() {
    for (const platform of ['wechat', 'qq']) {
        const data = await fetchJSON(`/messages?limit=50&platform=${platform}`);
        const items = (data && Array.isArray(data.messages)) ? data.messages : [];
        items.forEach((m) => {
            const time = m.time ? new Date(m.time).toLocaleTimeString() : '';
            if (m.direction === 'in') {
                addFeedItem(platform, 'in', time, `[${m.room}] ${m.sender}: ${m.content}`);
            } else {
                addFeedItem(platform, 'out', time, `[${m.room}] 回复: ${m.content}`);
            }
        });
    }
}

// === 绑定 ===
function bindEvents() {
    document.getElementById('btn-start-all').addEventListener('click', startAll);
    document.getElementById('btn-stop-all').addEventListener('click', stopAll);
    document.getElementById('btn-start-wx').addEventListener('click', () => startPlatform('wechat'));
    document.getElementById('btn-stop-wx').addEventListener('click', () => stopPlatform('wechat'));
    document.getElementById('btn-start-qq').addEventListener('click', () => startPlatform('qq'));
    document.getElementById('btn-stop-qq').addEventListener('click', () => stopPlatform('qq'));
    document.getElementById('btn-start-wecom').addEventListener('click', () => startPlatform('wecom'));
    document.getElementById('btn-stop-wecom').addEventListener('click', () => stopPlatform('wecom'));

    document.getElementById('btn-add-group').addEventListener('click', () => addGroup('wechat'));
    document.getElementById('btn-add-group-qq').addEventListener('click', () => addGroup('qq'));

    ['group-name-input', 'qq-group-input'].forEach((id) => {
        const input = document.getElementById(id);
        if (!input) return;
        input.addEventListener('keydown', (event) => {
            if (event.key === 'Enter') {
                event.preventDefault();
                addGroup(id === 'qq-group-input' ? 'qq' : 'wechat');
            }
        });
    });

    document.getElementById('btn-save-qq').addEventListener('click', saveQQConfig);
    document.getElementById('btn-save-wecom').addEventListener('click', saveWecomConfig);

    document.getElementById('btn-save-voice').addEventListener('click', saveVoiceConfig);
    document.getElementById('btn-voice-preview').addEventListener('click', previewVoice);
    document.getElementById('btn-save-music').addEventListener('click', saveMusicConfig);
    document.getElementById('btn-test-music').addEventListener('click', testMusic);
    document.getElementById('btn-qq-song-play').addEventListener('click', () => playSongToQQ(false));
    document.getElementById('btn-qq-song-random').addEventListener('click', () => playSongToQQ(true));

    const previewInput = document.getElementById('voice-preview-text');
    if (previewInput) {
        previewInput.addEventListener('keydown', (event) => {
            if (event.key === 'Enter') {
                event.preventDefault();
                previewVoice();
            }
        });
    }

    document.getElementById('btn-apply-style').addEventListener('click', async () => {
        const name = document.getElementById('style-select').value;
        await fetchJSON('/styles/active', {
            method: 'POST',
            body: JSON.stringify({ name }),
        });
        setTimeout(loadStatus, 500);
    });

    document.getElementById('btn-test-api').addEventListener('click', async () => {
        const result = document.getElementById('test-result');
        if (result) result.textContent = '测试中...';
        const data = await fetchJSON('/ai/test', { method: 'POST' });
        if (result) {
            result.textContent = data
                ? (data.success ? `成功: ${data.message}` : `失败: ${data.message}`)
                : '请求失败';
        }
    });

    document.getElementById('btn-save-ai').addEventListener('click', async () => {
        const result = document.getElementById('config-result');
        const apiUrl = document.getElementById('config-api-url').value.trim();
        const apiKey = document.getElementById('config-api-key').value.trim();
        const model = document.getElementById('config-model').value.trim();
        const apiStyle = document.getElementById('config-api-style').value;
        if (result) result.textContent = '正在保存并应用配置...';

        const data = await fetchJSON('/config', {
            method: 'POST',
            body: JSON.stringify({
                api_url: apiUrl,
                api_key: apiKey,
                model,
                api_style: apiStyle,
            }),
        });
        if (data && data.success) {
            if (result) result.textContent = `${data.message}: ${data.model} / ${data.api_style}`;
            const key = document.getElementById('config-api-key');
            if (key) {
                key.value = '';
                key.placeholder = '已保存，留空则不修改';
            }
            loadStatus();
        } else if (result) {
            result.textContent = (data && data.message) || '保存失败';
        }
    });

    document.getElementById('btn-clear-ctx-wx').addEventListener('click', async () => {
        await fetchJSON('/context/clear', {
            method: 'POST',
            body: JSON.stringify({ platform: 'wechat' }),
        });
        addFeedItem('wechat', 'event', new Date().toLocaleTimeString(), '微信上下文已清除');
    });

    document.getElementById('btn-clear-ctx-qq').addEventListener('click', async () => {
        await fetchJSON('/context/clear', {
            method: 'POST',
            body: JSON.stringify({ platform: 'qq' }),
        });
        addFeedItem('qq', 'event', new Date().toLocaleTimeString(), 'QQ 上下文已清除');
    });
}

// === 初始化 ===
document.addEventListener('DOMContentLoaded', () => {
    initTabs();
    bindEvents();
    connectWS();
    loadStatus();
    loadStyles();
    loadAIConfig();
    loadGroups('wechat');
    loadQQConfig();
    loadQQRooms();
    loadWecomConfig();
    loadHistory();
    loadVoiceConfig();
    loadSongs();
    loadQqSongOptions();
    loadMusicConfig();

    statusTimer = setInterval(() => {
        loadStatus();
        if (currentPlatform === 'qq') {
            loadQQRooms();
            loadQqSongOptions();
        } else if (currentPlatform === 'wecom') {
            loadWecomConfig();
        }
    }, 10000);

    heartbeatTimer = setInterval(() => {
        if (ws && ws.readyState === WebSocket.OPEN) ws.send('ping');
    }, 30000);
});

window.addEventListener('beforeunload', disconnect);
