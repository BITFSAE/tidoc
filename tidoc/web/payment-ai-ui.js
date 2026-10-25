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
  function markup(cfg) {
    return `<section class="dev-subsection payment-ai-settings" aria-label="付款截图 AI 识别">
      <div class="dev-subsection-title">付款截图 AI 识别 <small>实验性</small></div>
      <div class="settings-row"><label class="settings-row-copy" for="paymentAiKey"><b>API Key</b><span><span data-ai-key-status></span> <button class="link-btn" type="button" data-ai-key-link>获取 Key</button></span></label>
        <div class="settings-row-controls payment-ai-key"><input id="paymentAiKey" type="password" placeholder="API Key" autocomplete="new-password" spellcheck="false"/>
          <button class="btn small" data-ai-save>保存</button><button class="btn small ghost" data-ai-clear ${cfg.key_configured ? '' : 'hidden'}>清除</button></div></div>
      <div class="settings-row"><div class="settings-row-copy"><b>添加截图时自动识别</b>
        <span>关闭后不会自动上传；仍可选中条目后在「云识别」里手动识别。按量计费。</span></div>
        <label class="switch-line"><input type="checkbox" aria-label="添加付款截图时自动识别" data-ai-enabled ${cfg.enabled ? 'checked' : ''}/><span>${cfg.enabled ? '已开启' : '已关闭'}</span></label></div>
      <div class="hint payment-ai-cost" data-ai-cost></div>
      <details class="settings-advanced"><summary>高级选项</summary><div class="payment-ai-options">
        <label class="form-row">服务商<select data-ai-provider><option value="deepseek">DeepSeek Flash（推荐）</option><option value="qwen">Qwen Flash</option><option value="custom">其他兼容服务</option></select></label>
        <label class="form-row">接口地址<input data-ai-base type="url" spellcheck="false" value="${esc(cfg.base_url)}"/></label>
        <label class="form-row">模型<input data-ai-model spellcheck="false" value="${esc(cfg.model)}"/></label>
        <label class="form-row">并发数量<input data-ai-concurrency type="number" min="1" max="64" step="1" value="${cfg.concurrency}"/><small>推荐 32；限流时调低。</small></label>
        <label class="form-row">截图要求<textarea data-ai-requirements rows="2" maxlength="2000" placeholder="可选，如：需有付款时间">${esc(cfg.requirements)}</textarea></label>
        <details><summary>费用估算</summary><p class="hint">DeepSeek 高峰：北京时间周一至周五 9–12 时、14–18 时，节假日除外。低谷半价。</p>
          <div class="hint" data-ai-pricing></div>
          <div data-ai-custom-prices>
            <label class="form-row">输入单价<input data-ai-input-price type="number" min="0" step="0.01" value="${cfg.input_price}"/><small>元 / 百万 Token</small></label>
            <label class="form-row">输出单价<input data-ai-output-price type="number" min="0" step="0.01" value="${cfg.output_price}"/><small>元 / 百万 Token</small></label>
          </div>
          <p class="hint">按近期用量估算，首次按每张 3000 输入、150 输出 Token；以服务商账单为准。</p>
        </details>
        <p class="hint">Key 保存在本机，不随绑定包、报账方案导出。</p>
      </div></details><div class="hint warn" data-ai-error role="alert" hidden></div>
    </section>`;
  }
  function bind(body, initial) {
    const root = body.querySelector('.payment-ai-settings');
    if (!root) return;
    let cfg = initial;
    const $ = selector => root.querySelector(selector);
    const field = name => $(`[data-ai-${name}]`);
    const openLink = url => Api.openExternalUrl(url).catch(error => toast(error.message, 'err'));
    function showProvider(provider) {
      field('custom-prices').hidden = provider !== 'custom';
      field('key-link').hidden = !presets[provider].keyUrl;
    }
    function render() {
      field('enabled').checked = cfg.enabled && cfg.key_configured;
      field('enabled').disabled = !cfg.key_configured;
      field('enabled').nextElementSibling.textContent = field('enabled').checked ? '已开启' : cfg.key_configured ? '已关闭' : '先保存 Key';
      field('provider').value = cfg.provider;
      field('key-status').textContent = (presets[cfg.provider].label || cfg.model || '兼容服务')
        + (cfg.key_configured ? ` · 已保存 ${cfg.key_masked || ''}` : ' · 未填写');
      $('#paymentAiKey').placeholder = cfg.key_configured ? '留空保留当前 Key' : 'API Key';
      field('clear').hidden = !cfg.key_configured;
      showProvider(cfg.provider);
      const p = cfg.pricing;
      const parts = [`${p.period}${['高峰', '低谷'].includes(p.period) ? '（北京时间）' : ''}`, `100 张约 ${money(cfg.estimated_per_100)}`];
      if (cfg.total_calls) parts.push(`累计调用 ${cfg.total_calls} 次 · 估算 ${money(cfg.total_estimated_cost)}`);
      field('cost').textContent = parts.join(' · ');
      const link = p.url ? ` · <a href="${esc(p.url)}" data-ai-price-link>官方价格</a>` : '';
      field('pricing').innerHTML = `${esc(p.beijing_time)}（北京） · 输入 ¥${p.input} / 输出 ¥${p.output}，每百万 Token · ${esc(p.updated)}${link}${p.calendar_known ? '' : '<br>本年度节假日未更新，按工作日保守估算。'}`;
      const priceLink = root.querySelector('[data-ai-price-link]');
      if (priceLink) priceLink.onclick = event => { event.preventDefault(); openLink(priceLink.href); };
    }
    function values() {
      return { provider: field('provider').value, base_url: field('base').value.trim(), model: field('model').value.trim(),
        concurrency: Number(field('concurrency').value), requirements: field('requirements').value.trim(),
        input_price: Number(field('input-price').value), output_price: Number(field('output-price').value) };
    }
    // enabled 为 null 表示只保存 Key 和选项，不改自动识别开关。
    async function save(enabled, clear = false) {
      field('error').hidden = true;
      field('save').disabled = true;
      field('enabled').disabled = true;
      try {
        cfg = await Api.savePaymentAiSettings({ ...values(), enabled: clear ? false : enabled === null ? cfg.enabled : enabled }, $('#paymentAiKey').value || null, clear);
        $('#paymentAiKey').value = '';
        State.paymentAiEnabled = cfg.enabled && cfg.available;
        State.paymentAiAvailable = cfg.available;
        applyOcrUiVisibility();
        render();
        toast(clear ? 'Key 已清除' : enabled !== null ? (cfg.enabled ? '已开启自动识别' : '已关闭自动识别') : '已保存', 'ok');
      } catch (error) {
        field('error').textContent = error.message; field('error').hidden = false;
        field('enabled').checked = cfg.enabled;
      } finally { field('save').disabled = false; field('enabled').disabled = !cfg.key_configured; }
    }
    field('provider').onchange = () => {
      const selected = field('provider').value;
      field('base').value = presets[selected].base_url;
      field('model').value = presets[selected].model;
      $('#paymentAiKey').value = '';
      field('key-status').textContent = '填写当前服务商的 Key';
      showProvider(selected);
    };
    field('key-link').onclick = event => {
      event.preventDefault();
      const url = presets[field('provider').value].keyUrl;
      if (url) openLink(url);
    };
    field('save').onclick = () => save(null);
    field('clear').onclick = () => save(false, true);
    field('enabled').onchange = () => save(field('enabled').checked);
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
