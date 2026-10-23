const $ = s => document.querySelector(s);
let editorRowSequence = 0;
let slips = [], settings = {}, suppliers = [], products = [], receiptOptions = [], receiptOptionRows = [], editing = null, returning = null, deleting = null, view = 'active';
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const today = () => new Date(Date.now() - new Date().getTimezoneOffset()*60000).toISOString().slice(0,10);
const total = (s, key) => s.items.reduce((n, i) => n + Number(i[key] || 0), 0);
const statusLabel = value => ({'부분반환':'부분완료','반환완료':'완료'})[value] || value;
const headerDateFormat = new Intl.DateTimeFormat('ko-KR', {timeZone:'Asia/Seoul',year:'numeric',month:'2-digit',day:'2-digit',weekday:'long'});
const headerTimeFormat = new Intl.DateTimeFormat('ko-KR', {timeZone:'Asia/Seoul',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'});
function updateHeaderClock() {
  const now = new Date();
  $('#headerClock').dateTime = now.toISOString();
  $('#headerDate').textContent = headerDateFormat.format(now);
  $('#headerTime').textContent = headerTimeFormat.format(now);
}
updateHeaderClock();
setInterval(updateHeaderClock, 1000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) updateHeaderClock(); });
const trackingCarriers = [
  {name:'CJ대한통운', aliases:['CJ대한통운','대한통운','CJ택배','CJ'], url:n => `https://trace.cjlogistics.com/web/detail.jsp?slipno=${n}`},
  {name:'한진택배', aliases:['한진택배','한진','HANJIN'], url:n => `https://www.hanjin.com/kor/CMS/DeliveryMgr/WaybillResult.do?mCode=MN038&wblnum=${n}`},
  {name:'롯데택배', aliases:['롯데택배','롯데','현대택배','LOTTE'], url:n => `https://www.lotteglogis.com/home/reservation/tracking/linkView?InvNo=${n}`},
  {name:'우체국택배', aliases:['우체국택배','우체국','우체국소포','우체국등기','EPOST'], url:n => `https://service.epost.go.kr/trace.RetrieveDomRigiTraceList.comm?sid1=${n}`},
  {name:'로젠택배', aliases:['로젠택배','로젠','LOGEN','ILOGEN'], url:n => `https://www.ilogen.com/web/personal/trace/${n}`}
];
const carrierKey = name => String(name || '').replace(/[\s()㈜]/g,'').toUpperCase();
const trackingCarrier = name => trackingCarriers.find(c => c.aliases.some(a => carrierKey(a) === carrierKey(name)));
const trackingDigits = number => String(number || '').replace(/[\s-]/g,'');
function trackingUrl(carrier, number) {
  const digits = trackingDigits(number);
  return carrier && /^\d+$/.test(digits) ? carrier.url(encodeURIComponent(digits)) : '';
}
function trackingControl(s) {
  if (s.status === '반환완료') return '';
  if (!String(s.trackingNumber || '').trim()) return '<button class="tracking-link" disabled title="수정에서 출고 송장번호를 입력하세요">송장번호 미등록</button>';
  const url = trackingUrl(trackingCarrier(s.courier), s.trackingNumber);
  return url ? `<a class="button tracking-link" href="${esc(url)}" target="_blank" rel="noopener noreferrer" aria-label="${esc(s.number)} 송장 조회 (새 창)">송장 조회 ↗</a>` : `<button class="tracking-link" data-track="${s.id}">송장 조회</button>`;
}
function openTracking(s) {
  if (!/^\d+$/.test(trackingDigits(s.trackingNumber))) return toast('송장번호는 숫자와 하이픈, 공백만 사용할 수 있습니다. 전표 수정에서 확인하세요.');
  $('#trackingNumber').value = s.trackingNumber;
  $('#trackingSlip').textContent = `${s.number} · ${s.courier || '택배사 미등록'}`;
  $('#trackingCarrier').innerHTML = '<option value="">택배사를 선택하세요</option>' + trackingCarriers.map(c => `<option>${esc(c.name)}</option>`).join('');
  $('#trackingCarrier').value = trackingCarrier(s.courier)?.name || '';
  updateTrackingLink();
  $('#trackingDialog').showModal();
}
function updateTrackingLink() {
  const url = trackingUrl(trackingCarrier($('#trackingCarrier').value), $('#trackingNumber').value);
  $('#trackingOpen').hidden = !url;
  if (url) $('#trackingOpen').href = url;
  else $('#trackingOpen').removeAttribute('href');
}
$('#trackingCarrier').onchange = updateTrackingLink;
async function api(path, data) {
  const r = await fetch(path, data ? {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)} : {});
  const j = await r.json();
  if (!r.ok) throw Error(j.error || '요청 실패');
  return j;
}
function toast(s) {
  $('#toast').textContent = s;
  $('#toast').classList.add('show');
  setTimeout(() => $('#toast').classList.remove('show'), 3500);
}
function completedSerialChanges(s) {
  if (s.status !== '반환완료') return '';
  const serials = value => String(value || '').split(/[,，\r\n]+/).map(v => v.trim()).filter(Boolean);
  const changed = s.items.filter(item => {
    const outbound = new Set(serials(item.serial));
    return outbound.size && serials(item.receivedSerial).some(value => !outbound.has(value));
  });
  if (!changed.length) return '';
  return `<details class="serial-changes"><summary><span class="serial-change-badge">시리얼 변경 · ${changed.length}품목</span></summary><div class="serial-change-list">${changed.map(item => `<div class="serial-change-item"><b>${esc(item.name)}</b><span>출고: ${esc(item.serial)}</span><span>입고: ${esc(item.receivedSerial)}</span></div>`).join('')}</div></details>`;
}
function elapsedIndicator(s) {
  const label = `${esc(s.elapsedDays)}일`;
  if (s.status === '반환완료') return label;
  const band = s.elapsedDays <= 7 ? 0 : s.elapsedDays <= 14 ? 1 : s.elapsedDays <= 21 ? 2 : 3;
  return `<span class="elapsed-days elapsed-band-${band}" title="${['0~7일','8~14일','15~21일','22일 이상'][band]}">${label}</span>`;
}
function slipActions(s) {
  return `<div class="row-action-buttons">${s.status !== '반환완료' ? `<div class="action-row"><button data-return="${s.id}" class="return-button">부분완료</button><button data-return-all="${s.id}" title="입고 시리얼을 입력한 뒤 남은 수량 전체를 반환 처리">완료</button></div>` : ''}<div class="action-row"><button data-edit="${s.id}">수정</button><button data-print="${s.id}">전표 출력</button><button data-delete="${s.id}" class="delete-button">삭제</button></div></div>`;
}
function openDelete(s) {
  if (!s) return;
  deleting = {id:s.id, version:s.version};
  $('#deleteSlipNumber').textContent = s.number;
  $('#deleteSlipDetails').textContent = `${s.date} · ${s.supplier} · ${statusLabel(s.status)} · ${s.items.length}품목`;
  $('#deleteDialog').showModal();
  $('#deleteCancel').focus();
}
$('#deleteForm').onsubmit = async e => {
  e.preventDefault();
  if (!deleting) return;
  const button = $('#deleteConfirm'); button.disabled = true;
  try {
    await api('/api/slips/delete', deleting);
    $('#deleteDialog').close();
    await reload();
    toast('전표를 삭제했습니다.');
  } catch (err) { toast(err.message); }
  finally { button.disabled = false; }
};
$('#deleteDialog').addEventListener('close', () => { deleting = null; });
const searchIndex = new WeakMap();
const searchText = value => String(value ?? '').normalize('NFKC').toLowerCase();
const compactSearchText = value => value.replace(/[\s\p{Dash_Punctuation}\u2212]+/gu, '');
function searchableSlip(s) {
  if (searchIndex.has(s)) return searchIndex.get(s);
  const events = s.returns || [];
  const serialValues = values => values.flatMap(value => String(value ?? '').split(/[,，\r\n]+/));
  const fields = {
    number: [s.number], supplier: [s.supplier], trackingNumber: [s.trackingNumber],
    name: s.items.map(i => i.name), qrCodeNumber: s.items.map(i => i.qrCodeNumber),
    outboundSerial: serialValues(s.items.map(i => i.serial)),
    receivedSerial: serialValues([...s.items.map(i => i.receivedSerial), ...events.flatMap(e => (e.items || []).map(i => i.receivedSerial))]),
    details: [s.note, s.result, ...s.items.flatMap(i => [i.fault, i.receiptTreatment, i.mbRepairReport]), ...events.flatMap(e => [e.result, e.mbRepairReport])],
    other: [s.date, s.shippedMonth, s.courier, s.phone, s.company, s.companyPhone, s.manager, s.kind, statusLabel(s.status), s.returned,
      ...(s.requesters || []), ...s.items.flatMap(i => [i.productionMonth, i.receivedDate]), ...events.map(e => e.date)]
  };
  fields.all = Object.values(fields).flat();
  fields.serial = [...fields.outboundSerial, ...fields.receivedSerial];
  const index = Object.fromEntries(Object.entries(fields).map(([key, values]) => [key, values.filter(v => v != null && v !== '').map(value => {
    const text = searchText(value);
    return {text, compact:compactSearchText(text)};
  })]));
  searchIndex.set(s, index);
  return index;
}
function matchesSearch(s, terms, field) {
  if (!terms.length) return true;
  const values = searchableSlip(s)[field] || [];
  return terms.every(term => values.some(value => value.text.includes(term.text) || (term.compact && value.compact.includes(term.compact))));
}
function render() {
  const q = searchText($('#search').value).trim(), status = $('#statusFilter').value;
  const terms = q ? q.split(/\s+/u).map(text => ({text, compact:compactSearchText(text)})) : [];
  const field = $('#searchField').value;
  const requester = $('#requesterFilter').value;
  const filtered = slips.filter(s => (!requester || (requester === 'none' ? !s.requesters.length : s.requesters.includes(requester))) && (view === 'all' || (s.status === '반환완료') === (view === 'completed')) && (!status || s.status === status) && matchesSearch(s, terms, field));
  $('#count').textContent = filtered.length + '건';
  document.querySelectorAll('[data-view]').forEach(b => b.classList.toggle('active', b.dataset.view === view));
  $('#stats').innerHTML = [
    ['전체 전표', slips.length, '누적 접수 내역'],
    ['진행 중', slips.filter(s => s.status !== '반환완료').length, '부분완료 포함 · 미반환 품목 관리'],
    ['완료', slips.filter(s => s.status === '반환완료').length, '모든 품목이 반환된 전표'],
    ['받는이', suppliers.length, '등록된 AS 처리 업체']
  ].map(([label,n,sub],i) => `<article><div>${label}<span class="stat-icon">${['▤','↗','✓','▦'][i]}</span></div><strong>${n}<small>${i===3?'개사':'건'}</small></strong><p>${sub}</p></article>`).join('');
  $('#rows').innerHTML = filtered.map(s => `<tr><td><strong>${esc(s.number)}</strong><small>${esc(s.date)}</small></td><td>${esc(s.supplier)}</td><td>${esc(s.kind)}${s.requesters.length ? `<small>${esc(s.requesters.join(' · '))}</small>` : ''}</td><td>${esc(s.items[0].name)}${s.items.length>1?` 외 ${s.items.length-1}건`:''}${completedSerialChanges(s)}</td><td>${esc(s.courier||'—')}<small>${esc(s.trackingNumber||'—')}</small>${trackingControl(s)}</td><td><b>${total(s,'returnedQty')}</b> / ${total(s,'qty')}<small>미반환 ${total(s,'remainingQty')}</small></td><td><span class="badge b${['접수','발송','처리중','반환완료','부분반환'].indexOf(s.status)}">${esc(statusLabel(s.status))}</span></td><td>${elapsedIndicator(s)}<small>${s.status === '반환완료' ? '반환까지' : '진행 중'}</small></td><td>${esc(s.result||'—')}<small>${esc(s.returned||'—')}</small></td><td class="row-actions">${slipActions(s)}</td></tr>`).join('');
  $('#empty').hidden = filtered.length > 0;
  const hasFilters = Boolean(q || requester || status);
  $('#empty h3').textContent = hasFilters ? '검색 결과가 없습니다' : view === 'completed' ? '완료된 전표가 없습니다' : '표시할 전표가 없습니다';
  $('#empty p').textContent = hasFilters ? '검색 대상과 검색어를 확인하거나 초기화를 누르세요. 진행·완료 건을 함께 찾으려면 전체 목록을 선택하세요.' : view === 'completed' ? '모든 품목이 반환되면 완료 목록으로 자동 이동합니다.' : '새 전표를 작성하거나 검색 조건을 확인하세요.';
}
async function reload() { slips = await api('/api/slips'); render(); }
function supplierOptions(selected = '') {
  const names = suppliers.map(s => s.name);
  $('#slipForm [name=supplier]').innerHTML = '<option value="">받는이를 선택하세요</option>' +
    suppliers.map(s => `<option value="${esc(s.name)}">${esc(s.name)}</option>`).join('') +
    (selected && !names.includes(selected) ? `<option value="${esc(selected)}">${esc(selected)} (기존 전표)</option>` : '');
  $('#slipForm [name=supplier]').value = selected;
}
$('#slipForm [name=supplier]').onchange = e => {
  const supplier = suppliers.find(s => s.name === e.target.value);
  if (supplier) {
    $('#slipForm [name=phone]').value = supplier.phone;
  }
};
function refreshProductSuggestions() {
  $('#productNames').innerHTML = products.map(p => `<option value="${esc(p.name)}"></option>`).join('');
}
function itemRow(i = {}) {
  const tr = document.createElement('tr');
  tr.dataset.rowKey = 'editor-item-' + (++editorRowSequence);
  tr.dataset.sourceIndex = Number.isInteger(i.sourceIndex) ? i.sourceIndex : '';
  tr.innerHTML = `<td><input data-key="name" aria-label="품명" list="productNames" maxlength="120" placeholder="목록에서 선택 또는 직접 입력" value="${esc(i.name||'')}" required></td>` +
    ['productionMonth','serial','qrCodeNumber','qty','fault'].map(k => `<td data-cell="${k}" ${k==='qrCodeNumber'?'data-gn-qr hidden':''}><input data-key="${k}" aria-label="${({productionMonth:'생산 년월',serial:'출고 시리얼번호',qrCodeNumber:'QR코드번호',qty:'수량',fault:'불량 증상'})[k]}" ${['serial','qrCodeNumber'].includes(k)?'autocomplete="off" spellcheck="false" enterkeyhint="next"':''} ${k==='productionMonth'?'type="month"':k==='qty'?'type="number" min="1" max="99999" required':'maxlength="'+(k==='qrCodeNumber'?500:k==='fault'?70:40)+'"'} value="${esc(i[k]??(k==='qty'?1:''))}"></td>`).join('') +
    '<td><button type="button" class="remove" aria-label="품목 삭제">×</button></td>';
  tr.querySelector('.remove').onclick = () => {
    if ($('#items').children.length > 1) {
      $('#editorReceiptSerialRows').querySelector(`[data-row-key="${tr.dataset.rowKey}"]`)?.remove();
      tr.remove(); syncEditorStatus();
    } else toast('품목은 최소 1개가 필요합니다.');
  };
  $('#items').append(tr);
  updateGnQrColumns();
  if (editing) addEditorReceiptCard(tr, i);
  tr.querySelector('[data-key=name]').oninput = () => {
    const card = $('#editorReceiptSerialRows').querySelector(`[data-row-key="${tr.dataset.rowKey}"]`);
    if (card) card.querySelector('strong').textContent = tr.querySelector('[data-key=name]').value;
  };
  tr.querySelector('[data-key=qty]').oninput = syncEditorStatus;
}
function updateGnQrColumns() {
  const enabled = $('#slipForm [name=requesters][value=GN]').checked;
  $('#slipForm').querySelectorAll('[data-gn-qr]').forEach(cell => { cell.hidden = !enabled; });
  $('#slipForm .item-table').classList.toggle('has-gn-qr', enabled);
  $('#editor').classList.toggle('gn-editor', enabled);
}
function updateRequesterSelection() {
  const choices = [...$('#slipForm').querySelectorAll('[name=requesters]')];
  const selected = choices.filter(input => input.checked);
  choices.forEach(input => { input.disabled = selected.length === 1 && !input.checked; });
  $('#requesterLegacyNotice').hidden = selected.length <= 1;
  updateGnQrColumns();
}
$('#slipForm').querySelectorAll('[name=requesters]').forEach(input => input.addEventListener('change', updateRequesterSelection));
let lastScannerTerminator = -Infinity;
$('#slipForm').addEventListener('input', () => { lastScannerTerminator = -Infinity; });
$('#slipForm').addEventListener('keydown', e => {
  if (e.isComposing || !(e.target instanceof HTMLInputElement)) return;
  const input = e.target, scannerField = ['serial','qrCodeNumber'].includes(input.dataset.key);
  if (!scannerField && input.dataset.key !== 'qty') return;
  const scanTab = e.key === 'Tab' && scannerField && !e.shiftKey && !e.ctrlKey && !e.altKey && !e.metaKey;
  if (e.key !== 'Enter' && !scanTab) return;
  if (['checkbox','radio','submit','button'].includes(input.type)) return;
  // A scanner terminator must never implicitly save the slip, even after focus moves.
  e.preventDefault();
  if (!scannerField || e.repeat || e.timeStamp - lastScannerTerminator < 100) return;
  lastScannerTerminator = e.timeStamp;
  const nextKey = input.dataset.key === 'serial' && $('#slipForm [name=requesters][value=GN]').checked ? 'qrCodeNumber' : 'qty';
  const next = input.closest('tr').querySelector(`[data-key=${nextKey}]`);
  next.focus(); next.select();
});
function showShippedMonth() {
  const date = $('#receiptDate').value;
  $('#shippedMonth').textContent = date ? Number(date.slice(5,7)) + '월' : '—월';
}
$('#receiptDate').oninput = () => {showShippedMonth(); updateEditorElapsed();};
$('#receiptDate').onchange = $('#receiptDate').oninput;
function addEditorReceiptEntry(card, entry = {}, eventIndex = null) {
  const row = document.createElement('div'); row.className = 'editor-receipt-entry';
  row.dataset.eventIndex = eventIndex ?? '';
  row.innerHTML = `<div class="form-grid"><label>입고일<input type="date" data-receipt-field="date" value="${esc(entry.date||today())}" required></label><label>입고수량<input type="number" min="1" max="99999" data-receipt-field="qty" value="${esc(entry.qty||1)}" required></label></div><label>변경된 입고 시리얼번호<textarea data-receipt-field="receivedSerial" maxlength="400" rows="3" placeholder="교환 시 입력 · 동일 제품은 빈칸">${esc(entry.receivedSerial||'')}</textarea></label><label class="mb-report-field">M/B 수리내역서 작성<textarea data-receipt-field="mbRepairReport" maxlength="2000" rows="3" placeholder="해당 입고내역의 M/B 수리 내용 (선택 사항)">${esc(entry.mbRepairReport||'')}</textarea></label><label>입고처리<select data-receipt-field="result">${receiptOptionsHtml(entry.result||'')}</select></label><button type="button" class="remove-receipt">입고내역 삭제</button>`;
  row.querySelector('.remove-receipt').onclick = () => { row.remove(); syncEditorStatus(); };
  row.querySelector('[data-receipt-field=qty]').oninput = syncEditorStatus;
  row.querySelector('[data-receipt-field=date]').oninput = updateEditorElapsed;
  card.querySelector('.editor-receipt-entries').append(row);
  syncEditorStatus();
}
function addEditorReceiptCard(tr, item) {
  const card = document.createElement('article'); card.className = 'editor-receipt-row'; card.dataset.rowKey = tr.dataset.rowKey;
  card.innerHTML = `<strong>${esc(item.name||'추가 품목')}</strong><div class="editor-receipt-entries"></div><button type="button" class="add-editor-receipt">입고내역 추가</button>`;
  $('#editorReceiptSerialRows').append(card);
  editing.returns.forEach((event,eventIndex) => {
    event.items.filter(x => x.index === item.sourceIndex).forEach(x => addEditorReceiptEntry(card,{...x,date:event.date,result:event.result,mbRepairReport:event.mbRepairReport},eventIndex));
  });
  syncEditorStatus();
  card.querySelector('.add-editor-receipt').onclick = () => {
    const received = [...card.querySelectorAll('[data-receipt-field=qty]')].reduce((sum,i) => sum + Number(i.value||0),0);
    addEditorReceiptEntry(card,{qty:Math.max(1,Number(tr.querySelector('[data-key=qty]').value)-received)});
  };
}
function syncEditorStatus() {
  if (!editing) return;
  const rows = [...$('#items').children];
  const counts = rows.map(tr => {
    const card = $('#editorReceiptSerialRows').querySelector(`[data-row-key="${tr.dataset.rowKey}"]`);
    return [Number(tr.querySelector('[data-key=qty]').value), [...(card?.querySelectorAll('[data-receipt-field=qty]')||[])].reduce((sum,input)=>sum+Number(input.value||0),0)];
  });
  const status = $('#slipForm [name=status]');
  if (counts.length && counts.every(([sent,received]) => sent > 0 && sent === received)) status.value='반환완료';
  else if (counts.some(([,received]) => received > 0)) status.value='부분반환';
  else if (['부분반환','반환완료'].includes(status.value)) status.value='접수';
  updateEditorElapsed();
}
function updateEditorElapsed() {
  if (!editing) return;
  const start = $('#receiptDate').value;
  const dates = [...$('#editorReceiptSerialRows').querySelectorAll('[data-receipt-field=date]')].map(i=>i.value).filter(Boolean);
  const koreaDate = new Date(Date.now()+9*3600000).toISOString().slice(0,10);
  const end = $('#slipForm [name=status]').value === '반환완료' && dates.length ? dates.sort().at(-1) : koreaDate;
  $('#editorElapsed').textContent = start ? `경과 ${Math.max(0,Math.round((Date.parse(end)-Date.parse(start))/86400000))}일` : '';
}
function openEditor(s = null) {
  editing = s;
  const f = $('#slipForm'); f.reset(); $('#items').innerHTML = ''; $('#editorReceiptSerialRows').innerHTML='';
  supplierOptions(s?.supplier); refreshProductSuggestions();
  $('#editorTitle').textContent = s ? '전표 수정 · ' + s.number : '전표 작성';
  $('#editorNumberField').hidden = !s;
  f.elements.number.disabled = !s;
  f.elements.status.innerHTML = ['접수','발송','처리중', ...(s?['부분반환','반환완료']:[])].map(v=>`<option value="${v}">${statusLabel(v)}</option>`).join('');
  f.elements.status.disabled = false;
  const source = s || settings;
  for (const [k,v] of Object.entries(source)) if (k !== 'requesters' && f.elements.namedItem(k)) f.elements.namedItem(k).value=v;
  f.querySelectorAll('[name=requesters]').forEach(c=>c.checked=Boolean(s?.requesters.includes(c.value)));
  if (s) s.items.forEach(itemRow); else {f.elements.date.value=today(); itemRow();}
  updateRequesterSelection(); lastScannerTerminator = -Infinity;
  f.elements.status.value = s ? s.status : '발송';
  showShippedMonth();
  $('#editorReceiptSerials').hidden = !s;
  $('#editorElapsed').textContent = s ? `경과 ${s.elapsedDays}일` : '';
  $('#itemLock').hidden = !s;
  $('#addItem').disabled=false;
  $('#editor').showModal();
}
$('#newBtn').onclick = () => openEditor();
$('#addItem').onclick = () => $('#items').children.length < 8 ? itemRow() : toast('품목은 최대 8개까지 가능합니다.');
$('#slipForm').onsubmit = async e => {
  e.preventDefault();
  const data = {...(editing || settings), ...Object.fromEntries(new FormData(e.target))};
  data.requesters = new FormData(e.target).getAll('requesters');
  if (data.requesters.length > 1) return toast('AS 의뢰업체는 GN 또는 대일 중 한 곳만 선택하세요.');
  const itemRows = [...$('#items').children];
  data.items = itemRows.map(tr=>({...Object.fromEntries([...tr.querySelectorAll('[data-key]')].map(i=>[i.dataset.key,i.value])),sourceIndex:tr.dataset.sourceIndex === '' ? null : Number(tr.dataset.sourceIndex)}));
  if (editing) data.receiptEdits = itemRows.flatMap((tr,index)=>{
    const card=$('#editorReceiptSerialRows').querySelector(`[data-row-key="${tr.dataset.rowKey}"]`);
    return [...card.querySelectorAll('.editor-receipt-entry')].map(row=>({index,eventIndex:row.dataset.eventIndex === '' ? null : Number(row.dataset.eventIndex),date:row.querySelector('[data-receipt-field=date]').value,qty:Number(row.querySelector('[data-receipt-field=qty]').value),result:row.querySelector('[data-receipt-field=result]').value,receivedSerial:row.querySelector('[data-receipt-field=receivedSerial]').value,mbRepairReport:row.querySelector('[data-receipt-field=mbRepairReport]').value}));
  });
  if (!data.company) return toast('먼저 보낸이 설정에서 업체명을 입력하세요.');
  const btn = e.submitter; btn.disabled = true;
  try { await api('/api/slips', data); $('#editor').close(); await reload(); toast('전표가 저장되었습니다.'); }
  catch (err) { toast(err.message); } finally { btn.disabled = false; }
};
$('#settingsBtn').onclick = () => {
  const f = $('#settingsForm'); f.reset();
  for (const [k,v] of Object.entries(settings)) if (f.elements.namedItem(k)) f.elements.namedItem(k).value = v;
  $('#settings').showModal();
};
$('#settingsForm').onsubmit = async e => {
  e.preventDefault();
  try {
    const next = Object.fromEntries(new FormData(e.target));
    await api('/api/settings', next); settings = next; $('#settings').close(); toast('업체 정보를 저장했습니다.');
  } catch (err) { toast(err.message); }
};
function matchesManagementSearch(value, query) {
  const text = value.toLocaleLowerCase(), q = query.trim().toLocaleLowerCase();
  const compact = q.replace(/[\s()-]/g, '');
  return text.includes(q) || (compact.length > 0 && text.replace(/[\s()-]/g, '').includes(compact));
}
function renderSuppliers() {
  const filtered = suppliers.filter(s => matchesManagementSearch(`${s.name} ${s.phone}`, $('#supplierSearch').value));
  $('#supplierSearchCount').textContent = `${filtered.length} / ${suppliers.length}개`;
  $('#supplierRows').innerHTML = filtered.map(s => `<tr><td>${esc(s.name)}</td><td>${esc(s.phone)}</td><td><button data-supplier-edit="${s.id}">수정</button></td></tr>`).join('') ||
    `<tr><td colspan="3">${suppliers.length ? '검색 결과가 없습니다.' : '등록된 받는이가 없습니다. 위에서 등록하세요.'}</td></tr>`;
}
$('#supplierSearch').oninput = renderSuppliers;
$('#suppliersBtn').onclick = () => {
  $('#supplierForm').reset(); $('#supplierSearch').value = ''; renderSuppliers(); $('#suppliers').showModal();
};
$('#supplierReset').onclick = () => { $('#supplierForm').reset(); $('#supplierForm [name=id]').value = ''; };
$('#supplierForm').onsubmit = async e => {
  e.preventDefault(); const btn = e.submitter; btn.disabled = true;
  try {
    await api('/api/suppliers', Object.fromEntries(new FormData(e.target)));
    suppliers = await api('/api/suppliers'); e.target.reset(); e.target.elements.id.value = '';
    renderSuppliers(); render(); toast('받는이를 저장했습니다.');
  } catch (err) { toast(err.message); } finally { btn.disabled = false; }
};
function renderProducts() {
  const filtered = products.filter(p => matchesManagementSearch(p.name, $('#productSearch').value));
  $('#productSearchCount').textContent = `${filtered.length} / ${products.length}개`;
  $('#productRows').innerHTML = filtered.map(p => `<tr><td>${esc(p.name)}</td><td><button data-product-edit="${p.id}">수정</button></td></tr>`).join('') ||
    `<tr><td colspan="2">${products.length ? '검색 결과가 없습니다.' : '등록된 품명이 없습니다. 위에서 추가하세요.'}</td></tr>`;
}
$('#productSearch').oninput = renderProducts;
function openProducts() {
  $('#productForm').reset(); $('#productSearch').value = ''; renderProducts(); $('#products').showModal();
}
$('#productsBtn').onclick = openProducts;
$('#manageProducts').onclick = openProducts;
$('#productReset').onclick = () => { $('#productForm').reset(); $('#productForm [name=id]').value = ''; };
$('#productForm').onsubmit = async e => {
  e.preventDefault(); const btn = e.submitter; btn.disabled = true;
  try {
    await api('/api/products', Object.fromEntries(new FormData(e.target)));
    products = await api('/api/products'); e.target.reset(); e.target.elements.id.value = '';
    renderProducts();
    refreshProductSuggestions();
    toast('품명을 저장했습니다.');
  } catch (err) { toast(err.message); } finally { btn.disabled = false; }
};
function renderReceiptOptionRows() {
  $('#receiptOptionRows').innerHTML = receiptOptionRows.map(row => `<tr><td>${esc(row.name)}</td><td><button data-receipt-option-edit="${row.id}">수정</button> <button data-receipt-option-delete="${row.id}">삭제</button></td></tr>`).join('') || '<tr><td colspan="2">등록된 항목이 없습니다. 위에서 추가하세요.</td></tr>';
}
function refreshReceiptSelections() {
  const select = $('#returnForm [name=result]'), current = select.value;
  select.innerHTML = receiptOptionsHtml(receiptOptions.includes(current) || select.disabled ? current : '');
  $('#editorReceiptSerialRows').querySelectorAll('[data-receipt-field=result]').forEach(input=>{ input.innerHTML=receiptOptionsHtml(input.value); });
  $('#returnHistory').querySelectorAll('[data-history-result]').forEach(input => {
    input.innerHTML = receiptOptionsHtml(input.value);
  });
}
async function reloadReceiptOptions() {
  receiptOptionRows = await api('/api/receipt-options/manage');
  receiptOptions = receiptOptionRows.map(row => row.name);
  renderReceiptOptionRows(); refreshReceiptSelections();
}
async function openReceiptOptions() {
  try { await reloadReceiptOptions(); $('#receiptOptionForm').reset(); $('#receiptOptionsManager').showModal(); }
  catch (err) { toast(err.message); }
}
$('#receiptOptionsBtn').onclick = openReceiptOptions;
$('#manageReceiptOptions').onclick = openReceiptOptions;
$('#receiptOptionReset').onclick = () => $('#receiptOptionForm').reset();
$('#receiptOptionForm').onsubmit = async e => {
  e.preventDefault(); const button = e.submitter; button.disabled = true;
  try {
    await api('/api/receipt-options', Object.fromEntries(new FormData(e.target)));
    await reloadReceiptOptions(); e.target.reset(); toast('입고처리 항목을 저장했습니다.');
  } catch (err) { toast(err.message); } finally { button.disabled = false; }
};
function receiptOptionsHtml(current = '') {
  return '<option value="">선택 안 함</option>' + receiptOptions.map(value => `<option value="${esc(value)}" ${value === current ? 'selected' : ''}>${esc(value)}</option>`).join('') +
    (current && !receiptOptions.includes(current) ? `<option selected value="${esc(current)}">${esc(current)} (기존 값)</option>` : '');
}
function openReturn(s, all = false) {
  returning = s;
  const done = s.status === '반환완료', form = $('#returnForm'); form.reset();
  form.elements.result.innerHTML = receiptOptionsHtml(done ? s.result : '');
  form.elements.date.value = today(); form.elements.date.min = s.date;
  $('#returnTitle').textContent = (done ? '반환 이력 · ' : all ? '완료 처리 · ' : '부분완료 처리 · ') + s.number;
  $('#returnItems').innerHTML = s.items.map((i,index) => {
    const selected = all && i.remainingQty > 0;
    return `<tr data-index="${index}">
      <td><label class="return-item-choice"><input type="checkbox" aria-label="${esc(i.name)} 반환 선택" ${selected ? 'checked' : ''} ${!i.remainingQty || all ? 'disabled' : ''}><b>${esc(i.name)}</b></label>
        <small>출고 시리얼: ${esc(i.serial||'—')}</small>
        <div class="return-counts">발송 ${i.qty} · 반환 완료 ${i.returnedQty} · <b>미반환 ${i.remainingQty}</b></div>
        <label class="return-quantity">이번 반환 수량 <input type="number" aria-label="${esc(i.name)} 이번 반환 수량" min="1" max="${i.remainingQty}" value="${i.remainingQty||0}" ${selected ? '' : 'disabled'} ${all ? 'readonly' : ''}></label>
      </td>
      <td class="return-serial-cell"><label>입고 시리얼번호 (교환품)<textarea class="received-serial" aria-label="${esc(i.name)} 입고 시리얼번호" maxlength="400" rows="3" placeholder="교환된 제품의 시리얼을 입력하세요" ${i.remainingQty ? '' : 'disabled'}></textarea></label><small>${i.remainingQty ? '여러 개는 쉼표 또는 줄바꿈으로 구분 · 수리 후 동일 제품이면 비워 두세요.' : '반환 완료된 품목입니다. 아래 반환 이력에서 입고 시리얼을 수정하세요.'}</small></td>
    </tr>`;
  }).join('');
  $('#returnItems').querySelectorAll('input[type=checkbox]').forEach(c => c.onchange = () => {
    c.closest('tr').querySelector('input[type=number]').disabled = !c.checked;
  });
  // Typing a replacement serial also selects that row for a partial return.
  $('#returnItems').querySelectorAll('textarea:not(:disabled)').forEach(input => input.oninput = () => {
    const row = input.closest('tr'), checkbox = row.querySelector('[type=checkbox]');
    if (!checkbox.disabled && input.value.trim()) { checkbox.checked = true; checkbox.onchange(); }
  });
  renderReturnHistory(s);
  $('#returnSave').hidden = done; $('#selectRemaining').hidden = done || all;
  $('#returnSave').textContent = all ? '완료 저장' : '선택 품목 부분완료 저장';
  form.elements.date.disabled = done; form.elements.result.disabled = done; form.elements.mbRepairReport.disabled = done;
  $('#returnDialog').showModal();
  $('#returnDialog').scrollTop = 0;
  if (!done) $('#returnItems textarea:not(:disabled)')?.focus({preventScroll:true});
}
function renderReturnHistory(s) {
  $('#returnHistory').innerHTML = s.returns.map((event, eventIndex) => `<div class="serial-history" data-event="${eventIndex}"><b>${esc(event.date)}</b>${event.items.map(x => `<label>${esc(s.items[x.index].name)} · 반환 ${x.qty}개<textarea data-history-index="${x.index}" maxlength="400" rows="2" placeholder="교환된 제품의 입고 시리얼">${esc(x.receivedSerial||'')}</textarea></label>`).join('')}<label class="mb-report-field">M/B 수리내역서 작성<textarea data-history-report maxlength="2000" rows="3" placeholder="이 반환 이력의 M/B 수리 내용 (선택 사항)">${esc(event.mbRepairReport||'')}</textarea></label><div class="history-heading history-bottom"><label>입고처리<select data-history-result>${receiptOptionsHtml(event.result||'')}</select></label><span>입고일 ${esc(event.date)}</span><button type="button" data-save-serials="${eventIndex}">입고정보 저장</button></div></div>`).join('') || '<p class="muted">아직 반환 이력이 없습니다.</p>';
}
$('#selectRemaining').onclick = () => {
  $('#returnItems').querySelectorAll('input[type=checkbox]:not(:disabled)').forEach(c => {
    c.checked = true; const n = c.closest('tr').querySelector('input[type=number]'); n.disabled = false; n.value = n.max; c.closest('tr').querySelector('textarea').disabled = false;
  });
};
async function saveReturn(s, data) {
  const result = await api('/api/returns', {id:s.id, version:s.version, ...data});
  await reload();
  toast(result.status === '반환완료' ? '완료 · 완료 목록으로 이동했습니다.' : '부분완료를 저장했습니다. 남은 품목은 진행 목록에서 관리하세요.');
}
$('#returnForm').onsubmit = async e => {
  e.preventDefault();
  const items = [...$('#returnItems').querySelectorAll('tr')].filter(tr => tr.querySelector('[type=checkbox]').checked).map(tr => ({index:Number(tr.dataset.index), qty:Number(tr.querySelector('[type=number]').value), receivedSerial:tr.querySelector('textarea').value}));
  if (!items.length) return toast('반환할 품목을 선택하세요.');
  const btn = e.submitter; btn.disabled = true;
  try { await saveReturn(returning, {date:e.target.elements.date.value, result:e.target.elements.result.value, mbRepairReport:e.target.elements.mbRepairReport.value, items}); $('#returnDialog').close(); }
  catch (err) { toast(err.message); } finally { btn.disabled = false; }
};
document.addEventListener('click', async e => {
  const b = e.target.closest('button'); if (!b) return;
  if (b.dataset.receiptOptionEdit) {
    const row = receiptOptionRows.find(r => r.id === Number(b.dataset.receiptOptionEdit));
    $('#receiptOptionForm [name=id]').value = row.id; $('#receiptOptionForm [name=name]').value = row.name;
  }
  if (b.dataset.receiptOptionDelete) {
    b.disabled = true;
    try {
      await api('/api/receipt-options/delete', {id:Number(b.dataset.receiptOptionDelete)});
      await reloadReceiptOptions(); $('#receiptOptionForm').reset(); toast('항목을 삭제했습니다. 기존 기록은 유지됩니다.');
    } catch (err) { toast(err.message); b.disabled = false; }
  }
  if (b.dataset.saveSerials !== undefined) {
    b.disabled = true;
    const event = b.closest('[data-event]');
    const items = [...event.querySelectorAll('[data-history-index]')].map(input => ({index:Number(input.dataset.historyIndex),receivedSerial:input.value}));
    try {
      returning = await api('/api/return-serials', {id:returning.id,version:returning.version,eventIndex:Number(b.dataset.saveSerials),result:event.querySelector('[data-history-result]').value,mbRepairReport:event.querySelector('[data-history-report]').value,items});
      await reload();
      const saved = returning.returns[Number(b.dataset.saveSerials)];
      event.querySelector('[data-history-report]').value = saved.mbRepairReport || '';
      event.querySelectorAll('[data-history-index]').forEach(input => {
        input.value = saved.items.find(x => x.index === Number(input.dataset.historyIndex)).receivedSerial || '';
      });
      b.disabled = false; toast('입고정보를 저장했습니다.');
    } catch (err) { toast(err.message); b.disabled = false; }
  }
  if (b.dataset.close) $('#'+b.dataset.close).close();
  if (b.dataset.view) { view = b.dataset.view; $('#statusFilter').value = ''; render(); }
  if (b.dataset.edit) openEditor(slips.find(s => s.id === Number(b.dataset.edit)));
  if (b.dataset.print) preview(slips.find(s => s.id === Number(b.dataset.print)));
  if (b.dataset.delete) openDelete(slips.find(s => s.id === Number(b.dataset.delete)));
  if (b.dataset.track) openTracking(slips.find(s => s.id === Number(b.dataset.track)));
  if (b.dataset.return) openReturn(slips.find(s => s.id === Number(b.dataset.return)));
  if (b.dataset.returnAll) openReturn(slips.find(s => s.id === Number(b.dataset.returnAll)), true);
  if (b.dataset.productEdit) {
    const product = products.find(p => p.id === Number(b.dataset.productEdit));
    for (const [k,v] of Object.entries(product)) $('#productForm').elements.namedItem(k).value = v;
  }
  if (b.dataset.supplierEdit) {
    const supplier = suppliers.find(s => s.id === Number(b.dataset.supplierEdit));
    for (const [k,v] of Object.entries(supplier)) if ($('#supplierForm').elements.namedItem(k)) $('#supplierForm').elements.namedItem(k).value = v;
  }
});
$('#search').oninput = render;
$('#searchField').onchange = render;
$('#statusFilter').onchange = render;
$('#requesterFilter').onchange = render;
$('#resetSearch').onclick = () => {
  $('#search').value = '';
  $('#searchField').value = 'all';
  $('#statusFilter').value = '';
  $('#requesterFilter').value = '';
  render();
  $('#search').focus();
};
// Use the same physical half-sheet dimensions for the proof and printed copies.
function fitSlips(root) {
  root.querySelectorAll('.slip').forEach(slip => {
    const content = slip.querySelector('.slip-content');
    const style = getComputedStyle(slip);
    const width = slip.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    const height = slip.clientHeight - parseFloat(style.paddingTop) - parseFloat(style.paddingBottom);
    const apply = scale => {
      // Compensate for scaling so the table keeps using the full printable width.
      // This lets long text reflow and preserves a larger, more readable font.
      content.style.width = `${(width - 1) / scale}px`;
      content.style.transform = `scale(${scale})`;
      return Math.max(content.scrollHeight * scale, content.getBoundingClientRect().height) <= height - 1
        && content.scrollWidth * scale <= width;
    };
    if (apply(1)) return;
    let low = 0.01, high = 1;
    for (let i = 0; i < 16; i++) {
      const mid = (low + high) / 2;
      if (apply(mid)) low = mid; else high = mid;
    }
    apply(low);
  });
}
function preparePrint() {
  const paper = $('#paper');
  if (!paper.children.length) return false;
  let root = $('#print-root');
  if (!root) {
    root = document.createElement('div');
    root.id = 'print-root';
    root.setAttribute('aria-hidden', 'true');
    document.body.append(root);
  }
  root.innerHTML = paper.innerHTML;
  // Measure at the same width even before the browser enters print media.
  root.style.display = 'block';
  fitSlips(root);
  root.style.removeProperty('display');
  return true;
}
function preview(s) {
  const party = (title, company, phone, manager = '') => `<div class="party"><b>${title}</b><div><strong>${esc(company||'—')}</strong><p>전화번호 ${esc(phone||'—')}</p>${manager ? `<p>담당자 ${esc(manager)}</p>` : ''}</div></div>`;
  const copy = label => `<section class="slip"><div class="slip-content">
    <div class="slip-title"><div><h1>AS 입출고 전표</h1><p>${esc(s.kind)} · ${esc(statusLabel(s.status))}</p></div><span>${label} 보관용</span></div>
    <div class="slip-meta"><strong>${esc(s.number)}</strong><span>${s.requesters?.length ? `의뢰업체 ${esc(s.requesters.join(' · '))} · ` : ''}접수일 (출고일) ${esc(s.date)}</span></div>
    <div class="parties">${party('받는이',s.supplier,s.phone)}${party('보낸이',s.company,s.companyPhone,s.manager)}</div>
    <table class="print-table"><thead><tr><th>품명</th><th>출고 시리얼번호</th><th>수량</th><th>불량 증상</th></tr></thead><tbody>${s.items.map(i=>`<tr><td>${esc(i.name)}</td><td>${esc(i.serial||'')}</td><td>${esc(i.qty)}<br><small>반환 ${i.returnedQty}</small></td><td>${esc(i.fault)}</td></tr>`).join('')}</tbody></table>
    <div class="slip-result"><span>합계 수량 <b>${s.items.reduce((n,i)=>n+Number(i.qty),0)}</b></span></div>
    <div class="signatures"><span>인계자 __________________ (서명)</span><span>인수자 __________________ (서명)</span></div>
  </div></section>`;
  $('#paper').innerHTML = copy('받는이') + '<div class="cut">절 취 선</div>' + copy('보낸이');
  $('#preview').showModal();
  fitSlips($('#paper'));
  document.fonts.ready.then(() => { if ($('#preview').open) fitSlips($('#paper')); });
}
window.addEventListener('beforeprint', preparePrint);
$('#printBtn').onclick = async () => {
  await document.fonts.ready;
  if (preparePrint()) window.print();
};
(async () => {
  try { settings = await api('/api/settings'); suppliers = await api('/api/suppliers'); products = await api('/api/products'); receiptOptions = await api('/api/receipt-options'); await reload(); }
  catch (e) { toast('서버 연결 오류: ' + e.message); }
})();
