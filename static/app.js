const $ = s => document.querySelector(s);
let slips = [], settings = {}, suppliers = [], editing = null, returning = null, view = 'active';
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const today = () => new Date(Date.now() - new Date().getTimezoneOffset()*60000).toISOString().slice(0,10);
const total = (s, key) => s.items.reduce((n, i) => n + Number(i[key] || 0), 0);
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
function render() {
  const q = $('#search').value.trim().toLowerCase(), status = $('#statusFilter').value;
  const filtered = slips.filter(s => (view === 'all' || (s.status === '반환완료') === (view === 'completed')) && (!status || s.status === status) && JSON.stringify(s).toLowerCase().includes(q));
  $('#count').textContent = filtered.length + '건';
  document.querySelectorAll('[data-view]').forEach(b => b.classList.toggle('active', b.dataset.view === view));
  $('#stats').innerHTML = [
    ['전체 전표', slips.length, '누적 접수 내역'],
    ['진행 중', slips.filter(s => s.status !== '반환완료').length, '부분반환 포함 · 미반환 품목 관리'],
    ['반환 완료', slips.filter(s => s.status === '반환완료').length, '모든 품목이 반환된 전표'],
    ['공급사', suppliers.length, '등록된 거래 공급사']
  ].map(([label,n,sub],i) => `<article><div>${label}<span class="stat-icon">${['▤','↗','✓','▦'][i]}</span></div><strong>${n}<small>${i===3?'개사':'건'}</small></strong><p>${sub}</p></article>`).join('');
  $('#rows').innerHTML = filtered.map(s => `<tr><td><strong>${esc(s.number)}</strong><small>${esc(s.date)}</small></td><td>${esc(s.supplier)}</td><td>${esc(s.kind)}</td><td>${esc(s.items[0].name)}${s.items.length>1?` 외 ${s.items.length-1}건`:''}</td><td><b>${total(s,'returnedQty')}</b> / ${total(s,'qty')}<small>미반환 ${total(s,'remainingQty')}</small></td><td><span class="badge b${['접수','발송','처리중','반환완료','부분반환'].indexOf(s.status)}">${esc(s.status)}</span></td><td>${esc(s.returned||'—')}</td><td class="row-actions">${s.status!=='반환완료'?`<button data-return="${s.id}" class="return-button">반환</button> <button data-return-all="${s.id}" title="남은 수량을 오늘 날짜로 모두 반환 처리">전량 반환</button> `:`<button data-return="${s.id}">반환 이력</button> `}<button data-edit="${s.id}">수정</button> <button data-print="${s.id}">전표 출력</button></td></tr>`).join('');
  $('#empty').hidden = filtered.length > 0;
  $('#empty h3').textContent = view === 'completed' ? '완료된 전표가 없습니다' : '표시할 전표가 없습니다';
  $('#empty p').textContent = view === 'completed' ? '모든 품목이 반환되면 완료 목록으로 자동 이동합니다.' : '새 전표를 작성하거나 검색 조건을 확인하세요.';
}
async function reload() { slips = await api('/api/slips'); render(); }
function supplierOptions(selected = '') {
  const names = suppliers.map(s => s.name);
  $('#slipForm [name=supplier]').innerHTML = '<option value="">공급사를 선택하세요</option>' +
    suppliers.map(s => `<option value="${esc(s.name)}">${esc(s.name)}</option>`).join('') +
    (selected && !names.includes(selected) ? `<option value="${esc(selected)}">${esc(selected)} (기존 전표)</option>` : '');
  $('#slipForm [name=supplier]').value = selected;
}
$('#slipForm [name=supplier]').onchange = e => {
  const supplier = suppliers.find(s => s.name === e.target.value);
  if (supplier) {
    $('#slipForm [name=phone]').value = supplier.phone;
    $('#slipForm [name=supplierAddress]').value = supplier.address;
  }
};
function itemRow(i = {}) {
  const tr = document.createElement('tr');
  tr.innerHTML = ['name','model','serial','qty','fault'].map(k => `<td><input data-key="${k}" aria-label="${({name:'품목명',model:'모델명',serial:'시리얼번호',qty:'수량',fault:'불량 증상'})[k]}" ${k==='qty'?'type="number" min="1" max="99999" required':'maxlength="'+(k==='fault'?70:40)+'"'} ${k==='name'?'required':''} value="${esc(i[k]??(k==='qty'?1:''))}"></td>`).join('') + '<td><button type="button" class="remove" aria-label="품목 삭제">×</button></td>';
  tr.querySelector('.remove').onclick = () => {
    if ($('#items').children.length > 1) tr.remove(); else toast('품목은 최소 1개가 필요합니다.');
  };
  $('#items').append(tr);
}
function openEditor(s = null) {
  editing = s;
  const f = $('#slipForm'); f.reset(); $('#items').innerHTML = '';
  supplierOptions(s?.supplier);
  $('#editorTitle').textContent = s ? '전표 수정 · ' + s.number : '새 전표 작성';
  const locked = Boolean(s?.returns.length);
  f.elements.status.innerHTML = ['접수','발송','처리중', ...(locked?[s.status]:[])].map(v => `<option>${v}</option>`).join('');
  f.elements.status.disabled = locked;
  if (s) {
    for (const [k,v] of Object.entries(s)) if (f.elements.namedItem(k)) f.elements.namedItem(k).value = v;
    s.items.forEach(itemRow);
  } else { f.elements.date.value = today(); itemRow(); }
  $('#itemLock').hidden = !locked;
  $('#addItem').disabled = locked;
  $('#items').querySelectorAll('input').forEach(i => i.readOnly = locked);
  $('#items').querySelectorAll('button').forEach(b => b.disabled = locked);
  $('#editor').showModal();
}
$('#newBtn').onclick = () => openEditor();
$('#addItem').onclick = () => $('#items').children.length < 8 ? itemRow() : toast('품목은 최대 8개까지 가능합니다.');
$('#slipForm').onsubmit = async e => {
  e.preventDefault();
  const data = {...(editing || settings), ...Object.fromEntries(new FormData(e.target))};
  data.items = [...$('#items').children].map(tr => Object.fromEntries([...tr.querySelectorAll('input')].map(i => [i.dataset.key,i.value])));
  if (!data.company) return toast('먼저 내 업체 설정에서 업체명을 입력하세요.');
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
function renderSuppliers() {
  $('#supplierRows').innerHTML = suppliers.map(s => `<tr><td>${esc(s.name)}</td><td>${esc(s.phone)}</td><td>${esc(s.address)}</td><td><button data-supplier-edit="${s.id}">수정</button></td></tr>`).join('') || '<tr><td colspan="4">등록된 공급사가 없습니다. 위에서 등록하세요.</td></tr>';
}
$('#suppliersBtn').onclick = () => { $('#supplierForm').reset(); renderSuppliers(); $('#suppliers').showModal(); };
$('#supplierReset').onclick = () => { $('#supplierForm').reset(); $('#supplierForm [name=id]').value = ''; };
$('#supplierForm').onsubmit = async e => {
  e.preventDefault(); const btn = e.submitter; btn.disabled = true;
  try {
    await api('/api/suppliers', Object.fromEntries(new FormData(e.target)));
    suppliers = await api('/api/suppliers'); e.target.reset(); e.target.elements.id.value = '';
    renderSuppliers(); render(); toast('공급사를 저장했습니다.');
  } catch (err) { toast(err.message); } finally { btn.disabled = false; }
};
function openReturn(s) {
  returning = s;
  const done = s.status === '반환완료', form = $('#returnForm'); form.reset();
  form.elements.date.value = today(); form.elements.date.min = s.date;
  $('#returnTitle').textContent = (done ? '반환 이력 · ' : '품목 반환 · ') + s.number;
  $('#returnItems').innerHTML = s.items.map((i,index) => `<tr data-index="${index}"><td><input type="checkbox" aria-label="${esc(i.name)} 반환 선택" ${i.remainingQty ? '' : 'disabled'}></td><td>${esc(i.name)}<small>${esc(i.model)} / ${esc(i.serial)}</small></td><td>${i.qty}</td><td>${i.returnedQty}</td><td>${i.remainingQty}</td><td><input type="number" aria-label="${esc(i.name)} 이번 반환 수량" min="1" max="${i.remainingQty}" value="${i.remainingQty||0}" disabled></td></tr>`).join('');
  $('#returnItems').querySelectorAll('input[type=checkbox]').forEach(c => c.onchange = () => { c.closest('tr').querySelector('input[type=number]').disabled = !c.checked; });
  $('#returnHistory').innerHTML = s.returns.map(event => `<div class="history-entry"><b>${esc(event.date)}</b><span>${event.items.map(x => `${esc(s.items[x.index].name)} ${x.qty}개`).join(' · ')}</span><small>${esc(event.result||'')}</small></div>`).join('') || '<p class="muted">아직 반환 이력이 없습니다.</p>';
  $('#returnSave').hidden = done; $('#selectRemaining').hidden = done;
  form.elements.date.disabled = done; form.elements.result.disabled = done;
  $('#returnDialog').showModal();
}
$('#selectRemaining').onclick = () => {
  $('#returnItems').querySelectorAll('input[type=checkbox]:not(:disabled)').forEach(c => {
    c.checked = true; const n = c.closest('tr').querySelector('input[type=number]'); n.disabled = false; n.value = n.max;
  });
};
async function saveReturn(s, data) {
  const result = await api('/api/returns', {id:s.id, version:s.version, ...data});
  await reload();
  toast(result.status === '반환완료' ? '전량 반환 완료 · 완료 목록으로 이동했습니다.' : '일부 반환을 저장했습니다. 남은 품목은 진행 목록에서 관리하세요.');
}
$('#returnForm').onsubmit = async e => {
  e.preventDefault();
  const items = [...$('#returnItems').querySelectorAll('tr')].filter(tr => tr.querySelector('[type=checkbox]').checked).map(tr => ({index:Number(tr.dataset.index), qty:Number(tr.querySelector('[type=number]').value)}));
  if (!items.length) return toast('반환할 품목을 선택하세요.');
  const btn = e.submitter; btn.disabled = true;
  try { await saveReturn(returning, {date:e.target.elements.date.value, result:e.target.elements.result.value, items}); $('#returnDialog').close(); }
  catch (err) { toast(err.message); } finally { btn.disabled = false; }
};
document.addEventListener('click', async e => {
  const b = e.target.closest('button'); if (!b) return;
  if (b.dataset.close) $('#'+b.dataset.close).close();
  if (b.dataset.view) { view = b.dataset.view; $('#statusFilter').value = ''; render(); }
  if (b.dataset.edit) openEditor(slips.find(s => s.id === Number(b.dataset.edit)));
  if (b.dataset.print) preview(slips.find(s => s.id === Number(b.dataset.print)));
  if (b.dataset.return) openReturn(slips.find(s => s.id === Number(b.dataset.return)));
  if (b.dataset.returnAll) {
    b.disabled = true;
    try { await saveReturn(slips.find(s => s.id === Number(b.dataset.returnAll)), {date:today(), all:true}); }
    catch (err) { toast(err.message); b.disabled = false; }
  }
  if (b.dataset.supplierEdit) {
    const supplier = suppliers.find(s => s.id === Number(b.dataset.supplierEdit));
    for (const [k,v] of Object.entries(supplier)) $('#supplierForm').elements.namedItem(k).value = v;
  }
});
$('#search').oninput = render; $('#statusFilter').onchange = render;
function preview(s){const party=(title,company,phone,address,extra)=>`<div class="party"><b>${title}</b><div><strong>${esc(company||'—')}</strong><p>${esc(phone||'')}</p><p>${esc(address||'')}</p><p>${esc(extra||'')}</p></div></div>`;const copy=label=>`<section class="slip"><div class="slip-title"><div><h1>AS 입출고 전표</h1><p>${esc(s.kind)} · ${esc(s.status)}</p></div><span>${label} 보관용</span></div><div class="slip-meta"><strong>${esc(s.number)}</strong><span>접수일 ${esc(s.date)}</span></div><div class="parties">${party('공급자',s.supplier,s.phone,s.supplierAddress,'')}${party('공급받는자',s.company,s.companyPhone,s.address,[s.business,s.manager].filter(Boolean).join(' / '))}</div><table class="print-table"><thead><tr><th>품목 / 모델명</th><th>시리얼번호</th><th>수량</th><th>불량 증상</th></tr></thead><tbody>${s.items.map(i=>`<tr><td>${esc(i.name)}<br><small>${esc(i.model)}</small></td><td>${esc(i.serial)}</td><td>${esc(i.qty)}<br><small>반환 ${i.returnedQty}</small></td><td>${esc(i.fault)}</td></tr>`).join('')}</tbody></table><div class="slip-result"><span>합계 수량 <b>${s.items.reduce((n,i)=>n+Number(i.qty),0)}</b></span><span>반환일 ${esc(s.returned||'—')}</span><span>처리 결과 ${esc(s.result||'—')}</span></div><p class="slip-note"><b>비고</b> ${esc(s.note||'—')}</p><div class="signatures"><span>인계자 __________________ (서명)</span><span>인수자 __________________ (서명)</span></div></section>`;$('#paper').innerHTML=copy('공급자')+'<div class="cut">절 취 선</div>'+copy('공급받는자');$('#preview').showModal();}
$('#printBtn').onclick = () => window.print();
(async () => {
  try { settings = await api('/api/settings'); suppliers = await api('/api/suppliers'); await reload(); }
  catch (e) { toast('서버 연결 오류: ' + e.message); }
})();
