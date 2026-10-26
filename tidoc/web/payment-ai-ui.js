/* Payment screenshot recognition: settings and shared job progress. */
const PaymentAiUI = (() => {
  const presets = {
    deepseek: { base_url: 'https://api.deepseek.com', model: 'deepseek-flash', label: 'DeepSeek Flash（推荐）', keyUrl: 'https://platform.deepseek.com/api_keys' },
    qwen: { base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'qwen3.8-flash', label: 'Qwen Flash', keyUrl: 'https://bailian.console.aliyun.com/' },
    custom: { base_url: '', model: '', label: '', keyUrl: '' },
  };
  // Estimates are small; show cents, and say "under ¥0.01" instead of a row of zeros.
  const money = value => value == null ? '未设置单价' : !value ? '¥0'
    : value < 0.01 ? '不到 ¥0.01' : `¥${Number(value).toFixed(2)}`;
  // 每一项都用同一种行：左边说明，右边控件。改动即保存，只有 Key 需要点「保存」。
  function markup(cfg) {
    return `<section class="dev-subsection payment-ai-settings" aria-label="付款截图 AI 识别">
      <div class="dev-subsection-title">付款截图 AI 识别 <small>实验性</small></div>
      <div class="settings-row"><label class="settings-row-copy" for="paymentAiProvider"><b>服务商</b>
        <span>每个服务商的 Key 分开保存，切换后自动换用</span></label>
        <div class="settings-row-control"><select id="paymentAiProvider" class="settings-select" data-ai-provider>${Object.entries(presets)
          .map(([value, preset]) => `<option value="${value}">${esc(preset.label || '其他兼容服务')}</option>`).join('')}</select></div></div>
      <div class="settings-row"><label class="settings-row-copy" for="paymentAiKey"><b>API Key</b>
        <span><span data-ai-key-status></span><button class="link-btn" type="button" data-ai-key-link>获取 Key</button><button class="link-btn" type="button" data-ai-balance>查询余额</button></span></label>
        <div class="settings-row-control payment-ai-key"><input id="paymentAiKey" type="password" autocomplete="new-password" spellcheck="false"/>
          <button class="btn small" data-ai-save>保存</button><button class="btn small ghost" data-ai-clear>清除</button></div></div>
      <div class="settings-row"><div class="settings-row-copy"><b>添加截图时自动识别</b>
        <span>关闭后不会自动上传，仍可选中条目在「云识别」里手动识别</span></div>
        <label class="switch-line"><input type="checkbox" aria-label="添加付款截图时自动识别" data-ai-enabled ${cfg.enabled ? 'checked' : ''}/><span>${cfg.enabled ? '已开启' : '已关闭'}</span></label></div>
      <div class="settings-row"><div class="settings-row-copy"><b>费用估算</b><span class="payment-ai-cost" data-ai-cost></span></div>
        <button class="link-btn" type="button" data-ai-price-link>官方价格</button></div>
      <details class="settings-advanced payment-ai-advanced"><summary>高级选项</summary>
        <div class="settings-row"><label class="settings-row-copy" for="paymentAiBase"><b>接口地址</b><span>兼容 OpenAI 格式的 HTTPS 地址</span></label>
          <div class="settings-row-control"><input id="paymentAiBase" class="settings-input" data-ai-base type="url" spellcheck="false" placeholder="https://"/></div></div>
        <div class="settings-row"><label class="settings-row-copy" for="paymentAiModel"><b>模型</b><span>需支持图片输入</span></label>
          <div class="settings-row-control"><input id="paymentAiModel" class="settings-input" data-ai-model spellcheck="false"/></div></div>
        <div class="settings-row"><label class="settings-row-copy" for="paymentAiConcurrency"><b>并发数量</b><span>推荐 32；遇到限流时调低</span></label>
          <div class="settings-row-control"><input id="paymentAiConcurrency" class="settings-input narrow" data-ai-concurrency type="number" min="1" max="64" step="1"/></div></div>
        <div class="settings-row" data-ai-custom-prices><div class="settings-row-copy"><b>单价</b><span>元 / 百万 Token，用于估算费用</span></div>
          <div class="settings-row-control payment-ai-prices">
            <label>输入<input class="settings-input narrow" data-ai-input-price type="number" min="0" step="0.01"/></label>
            <label>输出<input class="settings-input narrow" data-ai-output-price type="number" min="0" step="0.01"/></label></div></div>
        <div class="settings-row is-stacked"><div class="payment-ai-prompt-head"><label class="settings-row-copy" for="paymentAiPrompt"><b>识别提示词 <small data-ai-prompt-state></small></b>
          <span>规定返回的字段和判断规则。修改时保留开头的 JSON 字段，否则识别会失败；改动后已识别的截图需要重新识别</span></label>
          <button class="btn small ghost" type="button" data-ai-prompt-reset>恢复默认</button></div>
          <textarea id="paymentAiPrompt" class="payment-ai-prompt" data-ai-prompt rows="9" maxlength="8000" spellcheck="false"></textarea>
          <label class="payment-ai-requirements" for="paymentAiRequirements"><span>附加要求 · 接在提示词末尾，恢复默认时保留</span>
            <textarea id="paymentAiRequirements" data-ai-requirements rows="2" maxlength="2000" placeholder="可选，如：需有付款时间"></textarea></label></div>
        <p class="payment-ai-note" data-ai-pricing></p>
      </details>
      <div class="hint warn" data-ai-error role="alert" hidden></div>
    </section>`;
  }
  function bind(body, initial) {
    const root = body.querySelector('.payment-ai-settings');
    if (!root) return;
    let cfg = initial;
    let balanceFor = null;  // 余额属于哪把 Key；换了 Key 或服务商就不再显示旧余额
    const $ = selector => root.querySelector(selector);
    const field = name => $(`[data-ai-${name}]`);
    const openLink = url => Api.openExternalUrl(url).catch(error => toast(error.message, 'err'));
    const providerName = provider => presets[provider].label.replace(/（.*）$/, '') || '其他兼容服务';
    function render() {
      const custom = cfg.provider === 'custom';
      field('provider').value = cfg.provider;
      field('base').value = cfg.base_url;
      field('model').value = cfg.model;
      field('concurrency').value = cfg.concurrency;
      field('input-price').value = cfg.input_price;
      field('output-price').value = cfg.output_price;
      field('requirements').value = cfg.requirements;
      field('prompt').value = cfg.prompt || cfg.default_prompt;
      field('prompt-reset').hidden = !cfg.prompt;
      field('prompt-state').textContent = cfg.prompt ? '已修改' : '默认';
      field('custom-prices').hidden = !custom;
      field('key-link').hidden = !presets[cfg.provider].keyUrl;
      field('key-status').textContent = cfg.key_configured ? `已保存 ${cfg.key_masked || ''}`
        : custom && !cfg.base_url ? '先在高级选项填写接口地址' : '未保存';
      field('key-status').classList.toggle('is-missing', !cfg.key_configured);
      $('#paymentAiKey').placeholder = cfg.key_configured ? '留空保留当前 Key' : `${providerName(cfg.provider)} API Key`;
      field('clear').hidden = !cfg.key_configured;
      field('balance').hidden = !(cfg.balance_supported && cfg.key_configured);
      if (balanceFor !== cfg.key_masked) { field('balance').textContent = '查询余额'; balanceFor = null; }
      field('enabled').checked = cfg.enabled && cfg.key_configured;
      field('enabled').disabled = !cfg.key_configured;
      field('enabled').nextElementSibling.textContent = field('enabled').checked ? '已开启' : cfg.key_configured ? '已关闭' : '先保存 Key';
      const p = cfg.pricing;
      const parts = cfg.estimated_per_100 == null ? ['在高级选项填写单价后显示']
        : [`${p.period}${['高峰', '低谷'].includes(p.period) ? '（北京时间）' : ''}`, `100 张约 ${money(cfg.estimated_per_100)}`];
      if (cfg.total_calls) parts.push(`累计调用 ${cfg.total_calls} 次，约 ${money(cfg.total_estimated_cost)}`);
      field('cost').textContent = parts.join(' · ');
      field('price-link').hidden = !p.url;
      const notes = [];
      if (p.known && !custom) notes.push(`当前单价：输入 ¥${p.input} / 输出 ¥${p.output}，每百万 Token，${esc(p.updated)} 核对。`);
      if (p.period === '高峰' || p.period === '低谷') notes.push('DeepSeek 高峰为北京时间周一至周五 9–12 时、14–18 时，节假日除外；低谷半价。'
        + (p.calendar_known ? '' : '本年度节假日未更新，按工作日估算。'));
      notes.push(`${cfg.estimate_basis === 'recent' ? `估算按本机已识别 ${cfg.estimate_count} 张的平均用量` : `还没有识别记录，估算按每张约 ${cfg.budget_tokens[0]} 输入、${cfg.budget_tokens[1]} 输出 Token`}，命中服务商缓存的部分按缓存价计；以服务商账单为准。Key 只保存在本机，不随绑定包、报账方案导出。`);
      field('pricing').innerHTML = notes.join('<br>');
    }
    function showError(message) {
      field('error').textContent = message;
      field('error').hidden = !message;
    }
    function values() {
      return { provider: field('provider').value, base_url: field('base').value.trim(), model: field('model').value.trim(),
        concurrency: Number(field('concurrency').value), requirements: field('requirements').value.trim(),
        prompt: field('prompt').value.trim(),
        input_price: Number(field('input-price').value), output_price: Number(field('output-price').value) };
    }
    // changes 覆盖当前表单值；只有开关传 enabled，其余保存不改自动识别。
    async function save(changes = {}, { key = null, clear = false, done = '' } = {}) {
      showError('');
      root.classList.add('is-saving');
      try {
        const wasEnabled = cfg.enabled;
        cfg = await Api.savePaymentAiSettings({ ...values(), ...changes }, key, clear);
        $('#paymentAiKey').value = '';
        State.paymentAiEnabled = cfg.enabled && cfg.available;
        State.paymentAiAvailable = cfg.available;
        applyOcrUiVisibility();
        const note = wasEnabled && !cfg.enabled && !('enabled' in changes) ? '，自动识别已关闭' : '';
        if (done) toast(done + note, 'ok');
      } catch (error) {
        showError(error.message);
      } finally {
        root.classList.remove('is-saving');
        render();
      }
    }
    field('provider').onchange = () => {
      const selected = field('provider').value;
      const preset = selected === 'custom' ? { base_url: cfg.custom_base_url, model: cfg.custom_model } : presets[selected];
      if (selected === 'custom') {
        $('.payment-ai-advanced').open = true;
        if (!preset.base_url) setTimeout(() => field('base').focus());
      }
      save({ base_url: preset.base_url, model: preset.model }, { done: `已切换到「${providerName(selected)}」` });
    };
    field('prompt-reset').onclick = () => save({ prompt: '' }, { done: '已恢复默认提示词' });
    for (const name of ['base', 'model', 'concurrency', 'requirements', 'prompt', 'input-price', 'output-price']) {
      field(name).onchange = () => save({}, { done: '已保存' });
    }
    // 只在点击时向 DeepSeek 查询，打开设置不联网。
    field('balance').onclick = async () => {
      const button = field('balance');
      button.disabled = true;
      button.textContent = '查询中…';
      try {
        const data = await Api.paymentAiBalance();
        const rows = data.balances.map(row => `${row.currency === 'CNY' ? '¥' : row.currency + ' '}${row.total}`);
        button.textContent = `余额 ${rows.join(' / ') || '¥0.00'}${data.available ? '' : '（不足）'} · 刷新`;
        button.title = data.balances.map(row => `充值 ${row.topped_up}，赠送 ${row.granted}`).join('；');
        balanceFor = cfg.key_masked;
        showError('');
      } catch (error) {
        button.textContent = '查询余额';
        showError(error.message);
      } finally { button.disabled = false; }
    };
    field('key-link').onclick = event => { event.preventDefault(); openLink(presets[cfg.provider].keyUrl); };
    field('price-link').onclick = () => cfg.pricing.url && openLink(cfg.pricing.url);
    const saveKey = () => {
      const key = $('#paymentAiKey').value.trim();
      if (!key) return showError('请填写 Key');
      save({}, { key, done: 'Key 已保存' });
    };
    field('save').onclick = saveKey;
    $('#paymentAiKey').onkeydown = event => { if (event.key === 'Enter') saveKey(); };
    field('clear').onclick = () => save({}, { clear: true, done: 'Key 已清除' });
    field('enabled').onchange = () => {
      const on = field('enabled').checked;
      save({ enabled: on }, { done: on ? '已开启自动识别' : '已关闭自动识别' });
    };
    render();
  }
  async function run(options, progress = null) {
    const owned = !progress;
    progress ||= taskProgress('付款截图 AI 识别：准备中…');
    try {
      const job = await Api.startPaymentAi(options.paths || null, options.entryIds || null);
      // Cancelling stops queued uploads; requests already sent finish and may still be billed.
      progress.cancelable?.(() => Api.cancelPaymentAi(job.id).catch(() => {}));
      let state;
      do {
        state = await Api.paymentAiProgress(job.id);
        const parts = [`付款截图 AI 识别 ${state.completed}/${state.total} 张`, `进行中 ${state.active}`, `排队 ${state.queued}`];
        if (state.failed) parts.push(`失败 ${state.failed}`);
        if (state.cancelled_count) parts.push(`已取消 ${state.cancelled_count}`);
        if (state.cached) parts.push(`缓存 ${state.cached}`);
        if (state.retrying) parts.push(`重试 ${state.retrying}`);
        parts.push(`${Math.round(state.elapsed)} 秒`, `估算 ${money(state.cost)}${state.unknown_cost ? ' 起' : ''}`);
        progress.update(parts.join(' · '));
        progress.fraction?.(state.completed, state.total);
        if (!state.done) await new Promise(resolve => setTimeout(resolve, 300));
      } while (!state.done);
      progress.cancelable?.(null);
      if (state.error) throw new Error(state.error);
      const failures = state.results.filter(row => row.error && !row.cancelled);
      if (failures.length) toast(`${failures.length} 张识别失败：${failures[0].error}`, 'err');
      else if (state.cancelled_count) toast(`已取消 ${state.cancelled_count} 张，可稍后在「云识别」中继续`, 'ok');
      return state;
    } finally {
      progress.cancelable?.(null);
      if (owned) progress.close();
    }
  }
  // Run recognition before saving so the backend only adopts results and never waits on the network.
  async function prepare(paths, progress = null) {
    if (!State.paymentAiEnabled || !paths?.length) return null;
    return (await run({ paths }, progress)).id;
  }
  async function enrich(infos, progress) {
    if (!State.paymentAiEnabled) return infos;
    const paths = infos.filter(row => row.type === 'payment_screenshot').map(row => row.path);
    if (!paths.length) return infos;
    const job = await run({ paths }, progress);
    const byPath = new Map(job.results.filter(row => !row.cancelled).map(row => [row.path, row]));
    return infos.map(info => {
      const row = byPath.get(info.path);
      return row ? { ...info, payment_ai_job_id: job.id, paid_amount: row.result?.amount || '', payment_ai: row.result,
        warning: [info.warning, row.error || row.message].filter(Boolean).join('；') } : info;
    });
  }
  // 云识别后的逐条结论：识别合计、是否已填实付、需要核对的地方。
  function summaryRows(job) {
    return (job?.entries || []).map((row) => {
      const bits = [`${row.count} 张`];
      if (row.recognized_total) bits.push(`合计 ${fmtMoney(row.recognized_total)}`);
      if (row.applied) bits.push(`已填实付 ${fmtMoney(row.applied)}`);
      const notes = row.notes || [];
      const state = notes.length ? 'warn' : 'ok';
      return `<button class="ocr-summary-row ${state}" data-ocr-goto="${esc(row.entry_id)}">
        <span class="ocr-summary-icon">${notes.length ? '!' : '✓'}</span>
        <span class="ocr-summary-label">${esc(row.label)}</span>
        <span class="ocr-summary-note">${esc([...bits, ...notes].join(' · '))}</span>
      </button>`;
    }).join('');
  }
  return { markup, bind, run, prepare, enrich, money, summaryRows };
})();
