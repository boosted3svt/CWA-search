'use strict';
const $ = id => document.getElementById(id);
let busy = false;
let currentJob = null;
let lastSearch = null;
let failedSelection = null;
function notice(message, error = false) { $('notice').textContent = message; $('notice').className = error ? 'error' : ''; }
function setBusy(value) { busy = value; document.querySelectorAll('button').forEach(button => { if (!['refresh-catalogs','stop-catalogs'].includes(button.id)) button.disabled = value; }); }
async function api(path, data) {
  const response = await fetch(path, data === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json','X-OPDS-UI':'1'},body:JSON.stringify(data)});
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || 'Request failed.');
  return value;
}
async function loadSettings() {
  const settings = await api('/api/settings');
  $('library-count').textContent = `${settings.library_count} libraries configured`;
  $('download-dir').value = settings.download_dir;
  if (settings.active_job && !busy) {
    currentJob = settings.active_job; setBusy(true);
    followJob({id:currentJob}, null, $('include-history').checked);
  }
  $('timeout-note').textContent = settings.prune_timeouts ? 'Timed-out libraries are removed automatically, with a backup. Use “Keep timed-out libraries” to prevent removal during a search.' : 'Automatic timeout removal is disabled in configuration.';
}
async function run(action, data) {
  if (busy) return;
  setBusy(true); notice(action === 'search' ? 'Searching your libraries…' : 'Working…');
  $('activity-panel').hidden = false; $('activity').textContent = '';
  try {
    if (action === 'search') { lastSearch = {...data}; $('download-error').hidden = true; }
    const job = await api(`/api/${action}`, data);
    currentJob = job.id;
    await followJob(job, action, data.include_history);
  } catch (error) { notice(error.message, true); setBusy(false); }
}
async function followJob(job, action, includeHistory) {
  $('activity-panel').hidden = false;
  try {
    let state;
    do {
      await new Promise(resolve => setTimeout(resolve, 700));
      state = await api(`/api/jobs/${job.id}`);
      $('stop-button').hidden = state.action !== 'search' || state.status !== 'running';
      $('stop-button').disabled = state.cancelled;
      $('activity').textContent = state.events.join('\n'); $('activity').scrollTop = $('activity').scrollHeight;
    } while (state.status === 'running');
    if (state.status === 'error') {
      if (state.action === 'download') showDownloadFailure(state);
      throw new Error(state.user_message || state.error);
    }
    if (state.action === 'search') lastSearch = state.query || lastSearch;
    if (state.action === 'download') markCard(state.selection, 'saved', '✓ Saved on server');
    if (state.action === 'search') showResults(state.results, includeHistory || state.include_history);
    notice(state.message || 'Completed.');
    await loadSettings();
  } catch (error) { notice(error.message, true); }
  finally { currentJob = null; $('stop-button').hidden = true; setBusy(false); }
}
function node(tag, text, className) { const element = document.createElement(tag); element.textContent = text; if (className) element.className = className; return element; }
function showResults(books, includeHistory) {
  $('results').replaceChildren(); $('result-heading').textContent = books.length ? `${books.length} matching ${books.length === 1 ? 'book' : 'books'}` : 'No eligible matches';
  for (const book of books) {
    const card = node('article', '', 'book'); card.dataset.bookId = book.key;
    const fallback = node('div', 'No cover available', 'cover placeholder');
    if (book.cover_url) {
      const image = document.createElement('img'); image.className = 'cover'; image.src = book.cover_url; image.alt = `Cover of ${book.title}`; image.loading = 'lazy'; image.addEventListener('error', () => image.replaceWith(fallback)); card.append(image);
    } else card.append(fallback);
    const details = node('div', ''); const badge = node('p', '', 'download-status'); badge.hidden = true; details.append(badge); details.append(node('h3', book.title), node('p', book.authors.join(', ') || 'Unknown author'));
    details.append(node('p', `${book.source} · ${book.library_id || 'Default library'}`, 'source'));
    if (book.cached_at) details.append(node('p', `Catalog refreshed: ${new Date(book.cached_at).toLocaleString()}${book.cache_refresh_error ? ' · Latest refresh failed; older data retained' : (book.cache_stale ? ' · Older cached data' : '')}`, 'source'));
    if (book.description) {
      const disclosure = document.createElement('details'); disclosure.append(node('summary', 'Description'));
      // OPDS descriptions may be HTML. Extract plain text without inserting it into the page.
      disclosure.append(node('p', book.description.replace(/<[^>]*>/g, ' '), 'description')); details.append(disclosure);
    }
    const label = node('label', 'Format'); const select = document.createElement('select'); select.setAttribute('aria-label', `Format for ${book.title}`);
    for (const format of book.formats) { const option = node('option', format.toUpperCase()); option.value = format; select.append(option); }
    select.value = book.preferred_format; label.append(select); details.append(label);
    const actions = node('div', '', 'actions');
    const download = node('button', 'Download to server', 'primary'); download.addEventListener('click', () => run('download', {id:book.key,format:select.value,include_history:includeHistory}));
    const bad = node('button', 'Reject this format'); bad.addEventListener('click', () => {
      if (confirm(`Skip ${select.value.toUpperCase()} for “${book.title}” in future searches?`)) run('mark-bad', {id:book.key,format:select.value,include_history:true});
    });
    actions.append(download, bad); details.append(actions); card.append(details); $('results').append(card);
  }
  setBusy(busy);
}
function markCard(selection, status, text) {
  if (!selection) return;
  for (const card of document.querySelectorAll('.book')) {
    if (card.dataset.bookId !== selection.id) continue;
    card.classList.remove('download-failed', 'download-saved'); card.classList.add(`download-${status}`);
    const badge = card.querySelector('.download-status'); badge.textContent = text; badge.hidden = false;
  }
}
function showDownloadFailure(state) {
  failedSelection = state.selection;
  markCard(state.selection, state.file_saved ? 'saved' : 'failed', state.file_saved ? '✓ File saved; follow-up failed' : '✕ Download failed — no new file saved');
  $('download-error').hidden = false;
  $('failure-heading').textContent = state.file_saved ? 'File saved — follow-up failed' : '✕ Download failed';
  $('failure-outcome').textContent = state.user_message;
  $('failure-selection').textContent = state.selection ? `${state.selection.title} · ${state.selection.source} · ${(state.selection.format || '').toUpperCase()}` : '';
  $('failure-details').textContent = state.error + (state.saved_path ? `\nSaved path: ${state.saved_path}` : '');
  $('find-another').hidden = Boolean(state.file_saved) || !state.selection;
  $('download-error').scrollIntoView({behavior:'smooth', block:'center'});
}
$('acknowledge-error').addEventListener('click', () => { $('download-error').hidden = true; notice('Failure acknowledged. The result remains marked; select another format or search for another copy.'); });
$('find-another').addEventListener('click', () => {
  if (!failedSelection || busy) return;
  const query = lastSearch || {author:$('author').value.trim(), title:$('title').value.trim() || failedSelection.title};
  run('search', {...query, include_history:false, skip_source:failedSelection.source});
});
$('search-form').addEventListener('submit', event => {
  event.preventDefault(); const author = $('author').value.trim(), title = $('title').value.trim();
  if (!author && !title) { notice('Enter a title or author.', true); return; }
  $('results').replaceChildren(); $('result-heading').textContent = 'Searching…';
  run('search', {author,title,exact:$('exact').checked,all_libraries:$('all-libraries').checked,include_history:$('include-history').checked,keep_timeouts:$('keep-timeouts').checked});
});
$('settings-form').addEventListener('submit', async event => {
  event.preventDefault(); if (busy) return; setBusy(true);
  try { await api('/api/settings', {download_dir:$('download-dir').value}); await loadSettings(); notice('Download folder saved.'); }
  catch (error) { notice(error.message, true); } finally { setBusy(false); }
});
$('stop-button').addEventListener('click', async () => {
  if (!currentJob) return;
  try { await api('/api/cancel', {id:currentJob}); $('stop-button').disabled = true; }
  catch (error) { notice(error.message, true); }
});
loadSettings().catch(error => notice(error.message, true));

async function loadCatalogs() {
  try {
    const state = await api('/api/catalogs');
    $('catalog-panel').hidden = !state.enabled;
    if (!state.enabled) return;
    const cached = state.libraries.filter(library => library.cached).length;
    const count = state.libraries.reduce((total, library) => total + library.book_count, 0);
    $('catalog-summary').textContent = `${state.running ? 'Refresh running. ' : ''}${count.toLocaleString()} cached books across ${cached}/${state.libraries.length} libraries. Refresh interval: ${state.interval_hours} hours. Searches use local metadata; downloads connect to the source server.`;
    $('refresh-catalogs').disabled = state.running;
    $('stop-catalogs').hidden = !state.running;
    $('catalog-events').textContent = state.events.join('\n');
    $('catalog-libraries').replaceChildren();
    for (const library of state.libraries) {
      const date = library.last_success ? new Date(library.last_success).toLocaleString() : 'not yet cached';
      $('catalog-libraries').append(node('p', `${library.source} · ${library.book_count.toLocaleString()} books · ${date}${library.error ? ' · Refresh failed: '+library.error : (library.stale && library.cached ? ' · Older cached data' : '')}`, 'source'));
    }
  } catch (error) { /* Foreground actions report connection failures separately. */ }
}
$('refresh-catalogs').addEventListener('click', async () => {
  try { await api('/api/catalogs/refresh', {}); await loadCatalogs(); }
  catch (error) { notice(error.message, true); }
});
$('stop-catalogs').addEventListener('click', async () => {
  try { await api('/api/catalogs/stop', {}); await loadCatalogs(); }
  catch (error) { notice(error.message, true); }
});
loadCatalogs(); setInterval(loadCatalogs, 5000);
