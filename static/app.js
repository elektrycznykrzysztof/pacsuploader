(() => {
  const TOKEN_KEY = 'plikownia-token';
  const state = { files: [], uploading: false, uploadMetadata: null };
  const byId = (id) => document.getElementById(id);
  const fileInput = byId('file-input');
  const dropZone = byId('drop-zone');
  const uploadButton = byId('upload-button');
  const authDialog = byId('auth-dialog');
  const tokenInput = byId('token-input');
  const studyDialog = byId('study-dialog');
  const studyForm = byId('study-form');
  const studySubmit = byId('study-submit');
  const studyPesel = byId('study-pesel');
  const nfsConnection = byId('nfs-connection');
  const nfsStatusText = byId('nfs-status-text');

  function playPingPong(video) {
    const frameStep = 1 / 24;
    let reversing = false;
    const play = () => video.play().catch(() => {});

    video.addEventListener('ended', () => {
      if (!Number.isFinite(video.duration) || video.duration <= frameStep) {
        video.currentTime = 0;
        play();
        return;
      }
      reversing = true;
      video.pause();
      video.currentTime = Math.max(0, video.duration - frameStep);
    });

    video.addEventListener('seeked', () => {
      if (!reversing) return;
      if (video.currentTime <= frameStep * 1.5) {
        reversing = false;
        video.currentTime = 0;
        play();
        return;
      }
      video.currentTime = Math.max(0, video.currentTime - frameStep);
    });

    play();
  }

  playPingPong(byId('top-animation'));

  function token() { return sessionStorage.getItem(TOKEN_KEY) || ''; }
  function headers(extra = {}) { return { ...extra, 'X-App-Token': token() }; }
  function formatBytes(value) {
    const size = Number(value) || 0;
    if (size < 1024) return `${size} B`;
    const units = ['KB', 'MB', 'GB', 'TB'];
    let scaled = size / 1024;
    let index = 0;
    while (scaled >= 1024 && index < units.length - 1) { scaled /= 1024; index += 1; }
    return `${new Intl.NumberFormat('pl-PL', { maximumFractionDigits: 1 }).format(scaled)} ${units[index]}`;
  }
  function formatDate(value) {
    if (!value) return '';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '';
    return new Intl.DateTimeFormat('pl-PL', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }).format(date);
  }
  function showMessage(text, isError = false) {
    const node = byId('upload-message');
    node.textContent = text;
    node.classList.toggle('error-message', isError);
  }

  function isConvertible(file) {
    return /\.(png|jpe?g|pdf)$/i.test(file.name || '');
  }

  function validPesel(value) {
    if (!/^\d{11}$/.test(value)) return false;
    const weights = [1, 3, 7, 9, 1, 3, 7, 9, 1, 3];
    const checksum = (10 - [...value.slice(0, 10)].reduce((sum, digit, index) => sum + Number(digit) * weights[index], 0) % 10) % 10;
    if (checksum !== Number(value[10])) return false;
    const year = Number(value.slice(0, 2));
    const encodedMonth = Number(value.slice(2, 4));
    const day = Number(value.slice(4, 6));
    for (const [offset, century] of [[80, 1800], [0, 1900], [20, 2000], [40, 2100], [60, 2200]]) {
      const month = encodedMonth - offset;
      if (month < 1 || month > 12) continue;
      const parsed = new Date(Date.UTC(century + year, month - 1, day));
      return parsed.getUTCFullYear() === century + year && parsed.getUTCMonth() === month - 1 && parsed.getUTCDate() === day;
    }
    return false;
  }

  function localDateTime(value) {
    const date = new Date(value);
    const pad = (number) => String(number).padStart(2, '0');
    return {
      date: `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`,
      time: `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`,
    };
  }

  function updateStudySubmit() {
    studyPesel.setCustomValidity(studyPesel.value && !validPesel(studyPesel.value) ? 'Wpisz prawidłowy PESEL.' : '');
    studySubmit.disabled = state.uploading || !studyForm.checkValidity();
  }

  function openStudyDialog({ reset = false, file = null } = {}) {
    if (reset) studyForm.reset();
    if (file) {
      const { date, time } = localDateTime(file.lastModified || Date.now());
      byId('study-date').value = date;
      byId('study-time').value = time;
    }
    byId('study-error').textContent = '';
    studyDialog.hidden = false;
    updateStudySubmit();
    setTimeout(() => byId('study-first-name').focus(), 0);
  }

  function closeStudyDialog() {
    studyDialog.hidden = true;
  }

  function renderNfsStatus(status) {
    nfsConnection.classList.remove('nfs-connected', 'nfs-disconnected', 'nfs-checking');
    if (status?.connected === true) {
      nfsConnection.classList.add('nfs-connected');
      nfsStatusText.textContent = 'Połączenie z serwerem';
    } else if (status?.state === 'checking' || status?.connected == null) {
      nfsConnection.classList.add('nfs-checking');
      nfsStatusText.textContent = 'Sprawdzam NFS…';
    } else {
      nfsConnection.classList.add('nfs-disconnected');
      nfsStatusText.textContent = 'Brak połączenia z NFS';
    }
  }

  async function refreshNfsStatus() {
    try {
      const response = await fetch('/api/health', { cache: 'no-store' });
      if (!response.ok) throw new Error('Nie można sprawdzić NFS.');
      const result = await response.json();
      renderNfsStatus(result.nfs);
    } catch (_) {
      renderNfsStatus({ connected: false, state: 'disconnected' });
    }
  }

  function openAuth(message = '') {
    authDialog.hidden = false;
    byId('auth-error').textContent = message;
    tokenInput.value = '';
    setTimeout(() => tokenInput.focus(), 0);
  }

  async function api(path, options = {}) {
    const response = await fetch(path, { ...options, headers: headers(options.headers || {}) });
    let body = {};
    try { body = await response.json(); } catch (_) { /* Empty response. */ }
    if (response.status === 401) {
      sessionStorage.removeItem(TOKEN_KEY);
      openAuth('Token jest nieprawidłowy lub wygasł.');
      throw new Error('Brak autoryzacji.');
    }
    if (!response.ok) throw new Error(body.error || 'Nie udało się wykonać operacji.');
    return body;
  }

  function addFiles(fileList) {
    const byPath = new Map(state.files.map((item) => [item.path, item]));
    let firstConvertible = null;
    for (const selected of Array.from(fileList || [])) {
      const file = selected.file || selected;
      const relativePath = selected.path || file.webkitRelativePath || file.name;
      byPath.set(relativePath, { file, path: relativePath });
      if (!firstConvertible && isConvertible(file)) firstConvertible = file;
    }
    state.files = Array.from(byPath.values());
    renderSelection();
    if (firstConvertible) {
      state.uploadMetadata = null;
      openStudyDialog({ reset: true, file: firstConvertible });
    }
  }

  function entryFile(entry) {
    return new Promise((resolve, reject) => entry.file(resolve, reject));
  }

  function readDirectoryBatch(reader) {
    return new Promise((resolve, reject) => reader.readEntries(resolve, reject));
  }

  async function collectDroppedEntry(entry, parentPath = '', files = []) {
    const relativePath = parentPath ? `${parentPath}/${entry.name}` : entry.name;
    if (entry.isFile) {
      files.push({ file: await entryFile(entry), path: relativePath });
      return files;
    }
    if (entry.isDirectory) {
      const reader = entry.createReader();
      while (true) {
        const batch = await readDirectoryBatch(reader);
        if (!batch.length) break;
        for (const child of batch) await collectDroppedEntry(child, relativePath, files);
      }
    }
    return files;
  }

  async function addDroppedItems(dataTransfer) {
    const items = Array.from(dataTransfer.items || []);
    const entries = items
      .filter((item) => item.kind === 'file' && typeof item.webkitGetAsEntry === 'function')
      .map((item) => item.webkitGetAsEntry())
      .filter(Boolean);

    if (!entries.length) {
      addFiles(dataTransfer.files);
      return;
    }

    const droppedFiles = [];
    for (const entry of entries) await collectDroppedEntry(entry, '', droppedFiles);
    addFiles(droppedFiles);
  }

  function renderSelection() {
    const selection = byId('selection');
    selection.hidden = state.files.length === 0;
    uploadButton.disabled = state.uploading || state.files.length === 0;
    if (!state.files.length) return;

    const count = state.files.length;
    const total = state.files.reduce((sum, item) => sum + item.file.size, 0);
    byId('selection-count').textContent = `${count} ${count === 1 ? 'plik' : count < 5 ? 'pliki' : 'plików'}`;
    byId('selection-size').textContent = formatBytes(total);
    const list = byId('file-list');
    list.replaceChildren();
    const visible = state.files.slice(0, 40);
    for (const item of visible) {
      const row = document.createElement('li');
      const icon = document.createElement('span');
      icon.className = 'file-icon';
      icon.textContent = '▧';
      const name = document.createElement('span');
      name.className = 'file-name';
      name.title = item.path;
      name.textContent = item.path;
      const size = document.createElement('span');
      size.className = 'file-size';
      size.textContent = formatBytes(item.file.size);
      row.append(icon, name, size);
      list.append(row);
    }
    if (state.files.length > visible.length) {
      const more = document.createElement('li');
      more.textContent = `… oraz ${state.files.length - visible.length} kolejnych plików`;
      list.append(more);
    }
  }

  byId('choose-files').addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', (event) => { addFiles(event.target.files); event.target.value = ''; });
  byId('clear-selection').addEventListener('click', () => {
    state.files = [];
    state.uploadMetadata = null;
    closeStudyDialog();
    renderSelection();
    showMessage('');
  });
  dropZone.addEventListener('click', (event) => { if (event.target === dropZone) fileInput.click(); });
  dropZone.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); fileInput.click(); }
  });
  for (const eventName of ['dragenter', 'dragover']) {
    dropZone.addEventListener(eventName, (event) => { event.preventDefault(); dropZone.classList.add('drag-over'); });
  }
  for (const eventName of ['dragleave', 'drop']) {
    dropZone.addEventListener(eventName, (event) => { event.preventDefault(); dropZone.classList.remove('drag-over'); });
  }
  dropZone.addEventListener('drop', async (event) => {
    try {
      await addDroppedItems(event.dataTransfer);
    } catch (error) {
      showMessage('Nie udało się odczytać upuszczonych plików lub katalogów.', true);
    }
  });

  function sendSelectedFiles(metadata = null) {
    if (!state.files.length || state.uploading) return;
    if (!token()) { openAuth(); return; }
    state.uploading = true;
    state.uploadMetadata = metadata;
    updateStudySubmit();
    renderSelection();
    closeStudyDialog();
    showMessage('');
    byId('upload-progress').hidden = false;
    byId('progress-bar').style.width = '0%';
    byId('progress-percent').textContent = '0%';
    byId('progress-detail').textContent = 'Przesyłanie plików na serwer…';

    const form = new FormData();
    for (const item of state.files) {
      form.append('files', item.file, item.file.name);
      form.append('paths', item.path);
    }
    if (metadata) {
      for (const [name, value] of Object.entries(metadata)) form.append(name, value);
    }
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/upload');
    xhr.setRequestHeader('X-App-Token', token());
    xhr.upload.addEventListener('progress', (event) => {
      if (!event.lengthComputable) return;
      const percent = Math.min(100, Math.round((event.loaded / event.total) * 100));
      byId('progress-bar').style.width = `${percent}%`;
      byId('progress-percent').textContent = `${percent}%`;
      byId('progress-detail').textContent = `${formatBytes(event.loaded)} z ${formatBytes(event.total)}`;
    });
    xhr.addEventListener('load', async () => {
      let body = {};
      try { body = JSON.parse(xhr.responseText); } catch (_) { /* Invalid response. */ }
      if (xhr.status === 401) {
        sessionStorage.removeItem(TOKEN_KEY);
        openAuth('Token jest nieprawidłowy lub wygasł.');
        showMessage('');
      } else if (xhr.status < 200 || xhr.status >= 300) {
        const errorMessage = body.error || 'Nie udało się przesłać plików.';
        showMessage(errorMessage, true);
        if (metadata && xhr.status === 400 && /PESEL|imię|nazwisko|rodzaj badania|stronę|urządzenia|Study Description|Data badania/i.test(errorMessage)) {
          state.uploadMetadata = null;
          openStudyDialog();
          byId('study-error').textContent = errorMessage;
        }
      } else {
        state.files = [];
        state.uploadMetadata = null;
        studyForm.reset();
        renderSelection();
        byId('progress-bar').style.width = '100%';
        byId('progress-percent').textContent = '100%';
        byId('progress-detail').textContent = 'Paczka jest już na serwerze i czeka na transfer.';
        const convertedImages = Number(body.job?.converted_images || 0);
        const convertedDocuments = Number(body.job?.converted_documents || 0);
        const conversions = [];
        if (convertedImages) conversions.push(`obrazy PNG/JPEG: ${convertedImages}`);
        if (convertedDocuments) conversions.push(`dokumenty PDF: ${convertedDocuments}`);
        showMessage(conversions.length
          ? `Paczka dodana do kolejki. Pliki przekonwertowane do DICOM (${conversions.join(', ')}).`
          : 'Paczka została dodana do kolejki transferów.');
        await refreshJobs();
        window.setTimeout(() => { byId('upload-progress').hidden = true; }, 2200);
      }
      state.uploading = false;
      updateStudySubmit();
      renderSelection();
    });
    xhr.addEventListener('error', () => {
      state.uploading = false;
      updateStudySubmit();
      renderSelection();
      showMessage('Nie udało się połączyć z serwerem. Sprawdź połączenie i spróbuj ponownie.', true);
    });
    xhr.send(form);
  }

  uploadButton.addEventListener('click', () => {
    if (!token()) { openAuth(); return; }
    if (!state.files.length || state.uploading) return;
    const hasConvertible = state.files.some((item) => isConvertible(item.file));
    if (hasConvertible && !state.uploadMetadata) {
      openStudyDialog();
      return;
    }
    sendSelectedFiles(state.uploadMetadata);
  });

  const handleStudyEdit = () => { byId('study-error').textContent = ''; updateStudySubmit(); };
  studyForm.addEventListener('input', handleStudyEdit);
  studyForm.addEventListener('change', handleStudyEdit);
  studyPesel.addEventListener('input', () => { studyPesel.value = studyPesel.value.replace(/\D/g, '').slice(0, 11); updateStudySubmit(); });
  const cancelStudy = () => { if (!state.uploading) closeStudyDialog(); };
  byId('study-cancel').addEventListener('click', cancelStudy);
  byId('study-cancel-bottom').addEventListener('click', cancelStudy);
  studyForm.addEventListener('submit', (event) => {
    event.preventDefault();
    updateStudySubmit();
    if (!studyForm.checkValidity()) { studyForm.reportValidity(); return; }
    const values = Object.fromEntries(new FormData(studyForm).entries());
    state.uploadMetadata = values;
    sendSelectedFiles(values);
  });

  function appendText(parent, tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    element.textContent = text;
    parent.append(element);
    return element;
  }

  function renderJobs(jobs, activeCount = null) {
    jobs = jobs.slice(0, 100);
    const list = byId('jobs-list');
    list.replaceChildren();
    byId('job-count').textContent = String(activeCount ?? jobs.filter((job) => job.state === 'transferring').length);
    if (!jobs.length) {
      const empty = document.createElement('div');
      empty.className = 'empty-state';
      appendText(empty, 'span', 'empty-icon', '↗');
      appendText(empty, 'strong', '', 'Tu pojawią się Twoje paczki');
      appendText(empty, 'p', '', 'Po dodaniu plików pokażemy postęp wysyłki.');
      list.append(empty);
      return;
    }

    const labels = { queued: 'W kolejce', transferring: 'Wysyłanie', waiting: 'Ponowienie', delivered: 'Wysłano' };
    for (const job of jobs) {
      const card = document.createElement('article');
      card.className = 'job-card';
      const top = document.createElement('div');
      top.className = 'job-top';
      const nameWrap = document.createElement('div');
      nameWrap.className = 'job-name-wrap';
      const title = appendText(nameWrap, 'div', 'job-name', job.name || 'Paczka');
      title.title = job.name || '';
      appendText(nameWrap, 'div', 'job-meta', `${job.files?.length || 0} plików · ${formatBytes(job.total_bytes)} · ${formatDate(job.created_at)}`);
      const status = appendText(top, 'span', `status-pill ${job.state || ''}`, labels[job.state] || 'Nieznany status');
      top.insertBefore(nameWrap, status);
      card.append(top);
      if (job.state === 'transferring') {
        const progress = document.createElement('div');
        progress.className = 'job-progress';
        appendText(progress, 'span', '', '');
        card.append(progress);
      }
      if (job.state === 'waiting' && job.error) {
        appendText(card, 'p', 'job-error', `Transfer zostanie ponowiony. ${job.error}`);
      }
      if (job.state === 'waiting') {
        const retry = appendText(card, 'button', 'retry-button', 'Wyślij ponownie teraz');
        retry.type = 'button';
        retry.addEventListener('click', async () => {
          retry.disabled = true;
          try { await api(`/api/jobs/${encodeURIComponent(job.id)}/retry`, { method: 'POST' }); await refreshJobs(); }
          catch (error) { showMessage(error.message, true); retry.disabled = false; }
        });
      }
      if (job.state === 'delivered' && job.destination) {
        appendText(card, 'div', 'job-meta', `Zapisano w: ${job.destination}`);
      }
      list.append(card);
    }
  }

  async function refreshJobs() {
    if (!token()) return;
    try {
      const result = await api('/api/jobs');
      renderJobs(result.jobs || [], result.active_count);
    } catch (error) {
      if (error.message !== 'Brak autoryzacji.') showMessage(error.message, true);
    }
  }
  byId('refresh-button').addEventListener('click', refreshJobs);
  refreshNfsStatus();
  window.setInterval(refreshNfsStatus, 5000);

  byId('auth-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    sessionStorage.setItem(TOKEN_KEY, tokenInput.value);
    try {
      const result = await api('/api/jobs');
      authDialog.hidden = true;
      renderJobs(result.jobs || [], result.active_count);
      showMessage('Połączono z serwerem.');
    } catch (error) {
      if (error.message !== 'Brak autoryzacji.') byId('auth-error').textContent = error.message;
    }
  });

  if (token()) refreshJobs();
  else openAuth();
  window.setInterval(refreshJobs, 5000);
})();
