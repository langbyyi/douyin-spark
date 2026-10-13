/* Keep localized causes in the shared JSON catalog, including legacy job results. */
(() => {
  let rules = [], loading;
  window.loadDeliveryFailureRules = () => {
    if (loading) return loading;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 3000);
    loading = fetch('/static/delivery-failure.json', { signal: controller.signal })
      .then(response => { if (!response.ok) throw new Error('无法加载失败原因'); return response.json(); })
      .then(value => { rules = value; })
      .catch(() => { loading = null; })
      .finally(() => clearTimeout(timer));
    return loading;
  };
  window.deliveryFailure = item => {
    if (item.failure) return item.failure;
    const outcome = String(item.outcome || ''), reason = String(item.reason || '');
    const rule = (['risk', 'logged_out'].includes(outcome) && rules.find(r => (r.outcomes || []).includes(outcome)))
      || rules.find(r => (r.outcomes || []).includes(outcome)
      || (r.tokens || []).some(token => reason.toLowerCase().includes(token))
      || (r.all_tokens?.length && r.all_tokens.every(token => reason.toLowerCase().includes(token)))) || {};
    let guidance = rule.guidance || '查看运行详情中的具体错误。';
    if (item.held || ['sending', 'unconfirmed'].includes(outcome)) guidance += '已暂停重复发送，防止消息重复。';
    if (item.retry_exhausted) guidance = '已达到当天尝试上限，请先解决失败原因。';
    return { label: '失败', reason: rule.reason || reason || '发送未完成', guidance,
      action: rule.action || 'logs', retry_allowed: !item.held && !item.retry_exhausted && ['failed', 'friend_error'].includes(outcome) };
  };
})();
