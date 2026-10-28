// ── Messengers: Telegram, WhatsApp, VK ──
//
// Two kinds of work live here. First messages to businesses that publish a
// messenger contact: Harvey wrote them, you send each one from your own
// app (Open copies the text and opens the chat), then say so. And
// conversations: in your bot, community or WhatsApp Business number, where
// Harvey sends what you approve, or in your own account, where you paste
// their answer in and copy Harvey's draft out.

let _msgr = null;

const M_STATUS = {
  open:           ['Открыт', 'active'],
  needs_human:    ['Нужны вы', 'waiting'],
  opted_out:      ['Отписался', 'bad'],
  closed:         ['Закрыт', 'idle'],
  pending_review: ['Ждёт вас', 'waiting'],
  held:           ['Задержано', 'bad'],
  approved:       ['Одобрено', 'good'],
  failed:         ['Не ушло', 'bad'],
  sent:           ['Отправлено', 'good'],
  skipped:        ['Пропущено', 'idle'],
  cancelled:      ['Отменено', 'idle'],
  scheduled:      ['Запланировано', 'active'],
};
const M_INTENT = {
  interested: 'Интересно', question: 'Вопрос', objection: 'Сомнение',
  not_interested: 'Отказ', unsubscribe: 'Не писать', escalate: 'Нужен человек',
  ooo: 'Автоответ', wrong_person: 'Не тот человек',
};
const M_CHANNEL = {telegram: 'Telegram', whatsapp: 'WhatsApp', vk: 'ВКонтакте'};

function mBadge(status) {
  const hit = own(M_STATUS, status) || [status, 'idle'];
  return '<span class="badge t-' + hit[1] + '">' + escHtml(hit[0]) + '</span>';
}

async function mPost(path, body) {
  return api(path, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body || {}),
  });
}

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch { /* below */ }
  const ta = document.createElement('textarea');
  ta.value = text; document.body.appendChild(ta); ta.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch { ok = false; }
  ta.remove();
  return ok;
}

async function loadMessengers() {
  const el = document.getElementById('messengers-body');
  const data = await api('/api/messengers');
  if (!data) { el.innerHTML = offlineState(); return; }
  if (data.error) { el.innerHTML = '<div class="banner bad">' + escHtml(data.error) + '</div>'; return; }
  _msgr = data;
  const nav = document.getElementById('nav-messengers');
  const waiting = data.queue.length + data.threads.filter(t =>
    t.status === 'needs_human' || (t.drafts || []).some(d => d.status !== 'approved')).length;
  if (nav) nav.textContent = waiting || '';

  let html = '';
  if (!data.enabled) {
    html += '<div class="banner wait">' + icon('alert') + '<div><b>Мессенджеры выключены</b>' +
      '<p>Включите <code>channels.messengers.enabled: true</code> в конфиге (harvey.local.yaml или harvey.yaml) ' +
      'и подтвердите сигналы Telegram contact, WhatsApp contact и VK page на вкладке Signals. ' +
      'Подробно: docs/messengers.md.</p></div></div>';
  }
  for (const p of data.problems || []) {
    html += '<div class="banner bad">' + icon('alert') + '<div><p>' + escHtml(p) + '</p></div></div>';
  }
  html += '<div class="stats-grid">' +
    statCard('Отправлено сегодня', data.sent_today + ' / ' + data.daily_cap) +
    statCard('Ждут отправки', data.queue.length) +
    statCard('Повторы по плану', data.scheduled.length) +
    statCard('Диалоги', data.threads.length) + '</div>';

  html += '<div class="subhead">Первые сообщения <span class="hint">Отправляете вы, со своего аккаунта. ' +
    '«Открыть» копирует текст и открывает чат; потом нажмите «Отправил».</span></div>';
  html += data.queue.length ? data.queue.map(coldCard).join('')
    : '<div class="card">' + emptyState(icon('check'), 'Очередь пуста',
        'Когда профилирование найдёт на сайтах ссылки на Telegram, WhatsApp или VK, Harvey напишет первые сообщения сюда.') + '</div>';

  html += '<div class="subhead">Диалоги</div>';
  html += data.threads.length ? data.threads.map(threadCard).join('')
    : '<div class="card">' + emptyState(icon('conversations'), 'Пока никто не написал',
        'Здесь появятся сообщения вашему боту, сообществу ВК или номеру WhatsApp Business, и ответы, которые вы вставите сами.') + '</div>';

  if (data.scheduled.length || data.done.length) {
    html += '<div class="subhead">История</div><div class="card">' +
      data.scheduled.concat(data.done).map(i =>
        '<div class="settings-row"><div><b>' + escHtml(i.company_name || i.company_domain || i.handle) + '</b>' +
        '<div class="muted">' + escHtml(i.channel_label) + ' · ' + escHtml(i.handle) + ' · шаг ' + i.step +
        (i.status === 'scheduled' ? ' · ' + relTime(i.send_at) : i.sent_at ? ' · ' + relTime(i.sent_at) : '') +
        (i.error ? ' · ' + escHtml(i.error) : '') + '</div></div>' + mBadge(i.status) + '</div>').join('') +
      '</div>';
  }
  el.innerHTML = html;
}

function statCard(label, value) {
  return '<div class="stat-card"><div class="label">' + escHtml(label) + '</div><div class="value">' +
    escHtml(String(value)) + '</div></div>';
}

function coldCard(i) {
  const who = i.company_name || i.company_domain || i.handle;
  const site = safeHref(i.company_website);
  const full = (_msgr && _msgr.sent_today >= _msgr.daily_cap);
  return '<div class="convo-card" id="cold-' + escHtml(i.id) + '"><div class="convo-head">' +
    '<span class="avatar">' + initials(who) + '</span><div><h3>' + escHtml(who) + '</h3><div class="sub">' + escHtml(i.channel_label) + ' · ' + escHtml(i.handle) +
      (i.company_location ? ' · ' + escHtml(i.company_location) : '') +
      (site ? ' · <a href="' + site + '" target="_blank" rel="noopener noreferrer">сайт</a>' : '') + '</div></div>' +
    '<div class="tags"><span class="tag">' + (i.step === 1 ? 'Первое' : 'Повтор') + '</span></div></div>' +
    (i.error ? '<div class="banner bad">' + icon('alert') + '<div><p>' + escHtml(i.error) + '</p></div></div>' : '') +
    '<textarea class="form-input" rows="5" id="cold-body-' + escHtml(i.id) + '">' + escHtml(i.body) + '</textarea>' +
    '<div class="convo-actions">' +
      '<button class="btn btn-primary btn-sm"' + (full ? ' disabled title="Дневной лимит исчерпан"' : '') +
        ' onclick="coldOpen(' + jsq(i.id) + ')">Открыть в ' + escHtml(i.channel_label) + '</button>' +
      '<button class="btn btn-secondary btn-sm" onclick="coldSent(' + jsq(i.id) + ')">Отправил</button>' +
      '<button class="btn btn-ghost btn-sm push" onclick="coldSave(' + jsq(i.id) + ')">Сохранить текст</button>' +
      '<button class="btn btn-ghost btn-sm" onclick="coldReplied(' + jsq(i.id) + ')">Ответили…</button>' +
      '<button class="btn btn-ghost btn-sm" onclick="coldSkip(' + jsq(i.id) + ')">Пропустить</button>' +
    '</div></div>';
}

async function coldSave(id, quiet) {
  const ta = document.getElementById('cold-body-' + id);
  const item = (_msgr.queue || []).find(x => x.id === id);
  if (!ta || (item && ta.value.trim() === item.body.trim())) return true;
  const data = await mPost('/api/messengers/cold/' + encodeURIComponent(id) + '/edit', {body: ta.value});
  if (data && data.success) { if (item) item.body = ta.value; if (!quiet) showToast('Сохранено', 'success'); return true; }
  showToast(failMsg(data, 'Не удалось сохранить'), 'error');
  return false;
}

async function coldOpen(id) {
  if (!await coldSave(id, true)) return;
  // The window opens now, inside the click, or the browser blocks it.
  const win = window.open('about:blank', '_blank');
  const data = await mPost('/api/messengers/cold/' + encodeURIComponent(id) + '/open');
  if (!data || !data.success) {
    if (win) win.close();
    showToast(failMsg(data, 'Не удалось открыть чат'), 'error');
    return;
  }
  const copied = await copyText(data.text);
  if (win) { win.opener = null; win.location.href = data.link; } else window.open(data.link, '_blank', 'noopener');
  showToast(data.prefills ? 'Текст уже в чате (и в буфере обмена). Отправьте, затем нажмите «Отправил».'
    : copied ? 'Текст в буфере обмена: вставьте его в чат, отправьте и нажмите «Отправил».'
    : 'Скопируйте текст вручную, отправьте и нажмите «Отправил».', 'success');
}

async function coldSent(id) {
  const data = await mPost('/api/messengers/cold/' + encodeURIComponent(id) + '/sent');
  if (data && data.success) showToast('Отмечено. Повтор появится здесь, если не ответят.', 'success');
  else showToast(failMsg(data, 'Не удалось отметить'), 'error');
  loadMessengers();
}

async function coldSkip(id) {
  const data = await mPost('/api/messengers/cold/' + encodeURIComponent(id) + '/skip');
  if (!(data && data.success)) showToast(failMsg(data, 'Не удалось пропустить'), 'error');
  loadMessengers();
}

async function coldReplied(id) {
  const text = window.prompt('Вставьте их ответ, и Harvey подготовит черновик ответа (можно оставить пустым):', '');
  if (text === null) return;
  const data = await mPost('/api/messengers/cold/' + encodeURIComponent(id) + '/replied', {text: text});
  if (data && data.success) showToast(text ? 'Повтор отменён. Черновик ответа появится в «Диалогах» после следующего цикла.' : 'Повтор отменён.', 'success');
  else showToast(failMsg(data, 'Не удалось сохранить'), 'error');
  loadMessengers();
}

function threadCard(t) {
  const name = t.display_name || t.company_name || t.handle || t.chat_id;
  const mine = t.transport === 'manual';
  const msgs = (t.messages || []).map(m =>
    '<div class="thread-msg ' + (m.direction === 'out' ? 'sent' : 'received') + '"><div class="sender">' +
    (m.direction === 'out' ? 'Вы' : escHtml(name)) + ' · ' + relTime(m.sent_at || m.created_at) + '</div>' +
    escHtml(m.body) + '</div>').join('');
  const drafts = (t.drafts || []).map(d =>
    '<div class="thread-draft"><div class="sender">' + icon('pen') + 'Черновик Harvey · ' + mBadge(d.status) + '</div>' +
    (d.error ? '<div class="muted">' + escHtml(d.error) + '</div>' : '') +
    '<textarea class="form-input" rows="4" id="mm-body-' + escHtml(d.id) + '">' + escHtml(d.body) + '</textarea>' +
    '<div class="convo-actions">' +
      (mine
        ? '<button class="btn btn-primary btn-sm" onclick="msgCopied(' + jsq(d.id) + ')">Скопировать и отметить отправленным</button>'
        : d.status === 'approved' ? '<span class="muted">Уйдёт в следующем цикле</span>'
        : '<button class="btn btn-primary btn-sm" onclick="msgApprove(' + jsq(d.id) + ')">Одобрить</button>') +
      (d.status !== 'approved' ? '<button class="btn btn-ghost btn-sm push" onclick="msgSave(' + jsq(d.id) + ')">Сохранить текст</button>' : '') +
      '<button class="btn btn-ghost btn-sm' + (d.status === 'approved' ? ' push' : '') + '" onclick="msgAction(' + jsq(d.id) + ',\'reject\')">Отклонить</button>' +
    '</div></div>').join('');
  const intent = t.intent && own(M_INTENT, t.intent) ? '<span class="tag">' + M_INTENT[t.intent] + '</span>' : '';
  const reason = t.status_reason && t.status !== 'open' ? '<div class="muted">' + escHtml(t.status_reason) + '</div>' : '';
  return '<div class="convo-card"><div class="convo-head">' +
    '<span class="avatar">' + initials(name) + '</span><div><h3>' + escHtml(name) + '</h3><div class="sub">' + escHtml(own(M_CHANNEL, t.channel) || t.channel) +
      (mine ? ' · ваш аккаунт' : ' · бот') + (t.handle ? ' · ' + escHtml(t.handle) : '') +
      (t.last_inbound_at ? ' · ' + relTime(t.last_inbound_at) : '') + '</div>' + reason + '</div>' +
    '<div class="tags">' + intent + mBadge(t.status) + '</div></div>' +
    '<div class="thread">' + (msgs || '<p class="muted">Сообщений пока нет.</p>') + drafts + '</div>' +
    '<div class="convo-actions"><span class="push"></span>' +
      (mine ? '<button class="btn btn-secondary btn-sm" onclick="threadPaste(' + jsq(t.id) + ')">Вставить их ответ…</button>' : '') +
      (t.status !== 'open' && t.status !== 'opted_out' ? '<button class="btn btn-ghost btn-sm" onclick="threadStatus(' + jsq(t.id) + ',\'open\')">Вернуть Harvey</button>' : '') +
      (t.status !== 'closed' && t.status !== 'opted_out' ? '<button class="btn btn-ghost btn-sm" onclick="threadStatus(' + jsq(t.id) + ',\'closed\')">Закрыть</button>' : '') +
      (t.status !== 'opted_out' ? '<button class="btn btn-ghost btn-sm" onclick="threadStatus(' + jsq(t.id) + ',\'opted_out\')">Больше не писать</button>' : '') +
    '</div></div>';
}

async function msgSave(id, quiet) {
  const ta = document.getElementById('mm-body-' + id);
  if (!ta) return true;
  const data = await mPost('/api/messengers/messages/' + encodeURIComponent(id) + '/edit', {body: ta.value});
  if (data && data.success) { if (!quiet) showToast('Сохранено', 'success'); return true; }
  showToast(failMsg(data, 'Не удалось сохранить'), 'error');
  return false;
}

async function msgApprove(id) {
  if (!await msgSave(id, true)) return;
  await msgAction(id, 'approve');
}

async function msgCopied(id) {
  const ta = document.getElementById('mm-body-' + id);
  if (!await msgSave(id, true)) return;
  const ok = ta ? await copyText(ta.value) : false;
  const data = await mPost('/api/messengers/messages/' + encodeURIComponent(id) + '/copied');
  if (data && data.success) showToast(ok ? 'Скопировано. Отправьте его в своём приложении.' : 'Отмечено. Скопируйте текст вручную.', 'success');
  else showToast(failMsg(data, 'Не удалось'), 'error');
  loadMessengers();
}

async function msgAction(id, action) {
  const data = await mPost('/api/messengers/messages/' + encodeURIComponent(id) + '/' + action);
  if (data && data.success) showToast(action === 'approve' ? 'Одобрено: уйдёт в следующем цикле.' : 'Готово', 'success');
  else showToast(failMsg(data, 'Не удалось'), 'error');
  loadMessengers();
}

async function threadPaste(id) {
  const text = window.prompt('Вставьте, что вам написали:', '');
  if (!text) return;
  const data = await mPost('/api/messengers/threads/' + encodeURIComponent(id) + '/paste', {text: text});
  if (data && data.success) showToast('Сохранено. Черновик ответа появится после следующего цикла.', 'success');
  else showToast(failMsg(data, 'Не удалось сохранить'), 'error');
  loadMessengers();
}

async function threadStatus(id, status) {
  const data = await mPost('/api/messengers/threads/' + encodeURIComponent(id) + '/status', {status: status});
  if (!(data && data.success)) showToast(failMsg(data, 'Не удалось'), 'error');
  loadMessengers();
}
