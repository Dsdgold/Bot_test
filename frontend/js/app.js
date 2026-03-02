// ===== STATE =====
let currentJobId = null;
let currentResults = [];
let pollingInterval = null;
let allSourcesSelected = true;

// ===== INIT =====
document.addEventListener('DOMContentLoaded', () => {
    loadSources();
    loadExports();
    loadJobs();
    setupEventListeners();
});

function setupEventListeners() {
    // Tab navigation
    document.querySelectorAll('.nav-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const tab = btn.dataset.tab;
            switchTab(tab);
        });
    });

    // Enter to search
    document.getElementById('searchInput').addEventListener('keydown', (e) => {
        if (e.key === 'Enter') startSearch();
    });
}

function switchTab(tab) {
    document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));

    document.querySelector(`[data-tab="${tab}"]`).classList.add('active');
    document.getElementById(`tab-${tab}`).classList.add('active');

    if (tab === 'exports') loadExports();
    if (tab === 'history') loadJobs();
}

// ===== SOURCES =====
async function loadSources() {
    try {
        const res = await fetch('/api/sources');
        const data = await res.json();
        const grid = document.getElementById('sourcesGrid');

        grid.innerHTML = data.sources.map(source => `
            <label class="source-card selected" data-source="${source.id}">
                <input type="checkbox" value="${source.id}" checked>
                <span class="source-name">${source.name}</span>
            </label>
        `).join('');

        // Toggle selection on click
        grid.querySelectorAll('.source-card').forEach(card => {
            card.addEventListener('click', (e) => {
                if (e.target.tagName === 'INPUT') {
                    card.classList.toggle('selected', e.target.checked);
                }
            });
        });
    } catch (err) {
        showToast('Nie udało się załadować źródeł', 'error');
    }
}

function toggleAllSources() {
    const checkboxes = document.querySelectorAll('#sourcesGrid input[type="checkbox"]');
    const cards = document.querySelectorAll('#sourcesGrid .source-card');
    allSourcesSelected = !allSourcesSelected;

    checkboxes.forEach(cb => cb.checked = allSourcesSelected);
    cards.forEach(card => card.classList.toggle('selected', allSourcesSelected));
}

function getSelectedSources() {
    const checked = document.querySelectorAll('#sourcesGrid input[type="checkbox"]:checked');
    return Array.from(checked).map(cb => cb.value);
}

// ===== SEARCH =====
async function startSearch() {
    const query = document.getElementById('searchInput').value.trim();
    if (!query) {
        showToast('Wpisz nazwę produktu do wyszukania', 'error');
        return;
    }

    const sources = getSelectedSources();
    if (sources.length === 0) {
        showToast('Wybierz przynajmniej jedno źródło', 'error');
        return;
    }

    const btn = document.getElementById('searchBtn');
    btn.disabled = true;
    btn.classList.add('loading');

    try {
        const res = await fetch('/api/search', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ query, sources }),
        });

        if (!res.ok) throw new Error('Search request failed');

        const data = await res.json();
        currentJobId = data.job_id;

        // Show results section
        document.getElementById('resultsSection').style.display = 'block';
        document.getElementById('progressContainer').style.display = 'block';
        document.getElementById('resultsStats').style.display = 'none';
        document.getElementById('tableContainer').style.display = 'none';

        showToast(`Rozpoczęto szukanie: "${query}"`, 'info');

        // Start polling for results
        startPolling(currentJobId);

    } catch (err) {
        showToast('Błąd podczas uruchamiania wyszukiwania', 'error');
    } finally {
        btn.disabled = false;
        btn.classList.remove('loading');
    }
}

function startPolling(jobId) {
    if (pollingInterval) clearInterval(pollingInterval);

    pollingInterval = setInterval(async () => {
        try {
            const res = await fetch(`/api/jobs/${jobId}`);
            const job = await res.json();

            updateProgress(job);

            if (job.status === 'completed' || job.status === 'failed') {
                clearInterval(pollingInterval);
                pollingInterval = null;

                if (job.status === 'completed') {
                    currentResults = job.results;
                    showResults(job);
                    showToast(`Znaleziono ${job.products_found} produktów!`, 'success');
                } else {
                    showToast('Wyszukiwanie zakończone z błędami', 'error');
                }
            }
        } catch (err) {
            console.error('Polling error:', err);
        }
    }, 1500);
}

function updateProgress(job) {
    const pct = job.total_sources > 0 ? (job.progress / job.total_sources) * 100 : 0;

    document.getElementById('progressFill').style.width = `${pct}%`;
    document.getElementById('progressText').textContent =
        job.status === 'completed' ? 'Zakończono!' :
        job.status === 'failed' ? 'Błąd!' :
        `Szukanie w toku... (${job.products_found} znalezionych)`;
    document.getElementById('progressCount').textContent =
        `${job.progress} / ${job.total_sources}`;
    document.getElementById('currentSource').textContent =
        job.current_source ? `Aktualnie: ${job.current_source}` : '';
}

function showResults(job) {
    document.getElementById('progressContainer').style.display = 'none';
    document.getElementById('resultsStats').style.display = 'grid';
    document.getElementById('tableContainer').style.display = 'block';

    // Stats
    document.getElementById('statTotal').textContent = job.products_found;
    document.getElementById('statSources').textContent = job.total_sources;

    // Find cheapest price
    const prices = job.results
        .map(p => parseFloat(p.cena))
        .filter(p => !isNaN(p) && p > 0);
    const cheapest = prices.length > 0 ? Math.min(...prices).toFixed(2) : '-';
    document.getElementById('statCheapest').textContent =
        cheapest !== '-' ? `${cheapest} zł` : '-';

    renderTable(job.results);
}

function renderTable(products) {
    const tbody = document.getElementById('resultsBody');

    if (products.length === 0) {
        tbody.innerHTML = `
            <tr>
                <td colspan="6" style="text-align:center; padding:40px; color:var(--text-muted)">
                    Brak wyników do wyświetlenia
                </td>
            </tr>
        `;
        return;
    }

    tbody.innerHTML = products.map(p => {
        const price = p.cena ? `${p.cena} zł` : 'Brak ceny';
        const priceClass = p.cena ? 'product-price' : 'product-price no-price';
        const imgSrc = p.zdjecie || '';
        const imgHtml = imgSrc
            ? `<img class="product-img" src="${escapeHtml(imgSrc)}" alt="" loading="lazy" onerror="this.style.display='none'">`
            : '<div class="product-img" style="background:var(--bg-tertiary)"></div>';

        // Build product details lines
        const details = [];
        if (p.producent) details.push(`<span class="detail-label">Producent:</span> ${escapeHtml(p.producent)}`);
        if (p.indeks) details.push(`<span class="detail-label">Indeks:</span> ${escapeHtml(p.indeks)}`);
        if (p.indeks_producenta) details.push(`<span class="detail-label">Indeks prod.:</span> ${escapeHtml(p.indeks_producenta)}`);
        if (p.jednostka) details.push(`<span class="detail-label">Jednostka:</span> ${escapeHtml(p.jednostka)}`);
        if (p.kategoria) details.push(`<span class="detail-label">Kategoria:</span> ${escapeHtml(p.kategoria)}`);
        if (p.dostepnosc) details.push(`<span class="detail-label">Dostępność:</span> ${escapeHtml(p.dostepnosc)}`);
        if (p.ocena) details.push(`<span class="detail-label">Ocena:</span> ${escapeHtml(p.ocena)}`);
        if (p.liczba_opinii) details.push(`<span class="detail-label">Opinii:</span> ${escapeHtml(p.liczba_opinii)}`);
        const detailsHtml = details.length > 0
            ? `<div class="product-details">${details.join('<br>')}</div>`
            : '<span style="color:var(--text-muted)">-</span>';

        return `
            <tr>
                <td>${imgHtml}</td>
                <td>
                    <div class="product-name">${escapeHtml(p.nazwa)}</div>
                    ${p.opis ? `<div class="product-desc">${escapeHtml(p.opis)}</div>` : ''}
                </td>
                <td><span class="${priceClass}">${escapeHtml(price)}</span></td>
                <td><span class="source-badge">${escapeHtml(p.zrodlo)}</span></td>
                <td>${detailsHtml}</td>
                <td>
                    ${p.url ? `<a href="${escapeHtml(p.url)}" target="_blank" rel="noopener" class="link-btn" title="Otwórz produkt">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path>
                            <polyline points="15 3 21 3 21 9"></polyline>
                            <line x1="10" y1="14" x2="21" y2="3"></line>
                        </svg>
                    </a>` : '-'}
                </td>
            </tr>
        `;
    }).join('');
}

// ===== FILTERING & SORTING =====
function filterResults() {
    const filter = document.getElementById('filterInput').value.toLowerCase();
    const filtered = currentResults.filter(p =>
        p.nazwa.toLowerCase().includes(filter) ||
        p.zrodlo.toLowerCase().includes(filter) ||
        (p.cena && p.cena.includes(filter)) ||
        (p.producent && p.producent.toLowerCase().includes(filter)) ||
        (p.indeks && p.indeks.toLowerCase().includes(filter)) ||
        (p.kategoria && p.kategoria.toLowerCase().includes(filter))
    );
    renderTable(filtered);
}

function sortResults() {
    const sort = document.getElementById('sortSelect').value;
    const sorted = [...currentResults];

    switch (sort) {
        case 'price_asc':
            sorted.sort((a, b) => {
                const pa = parseFloat(a.cena) || Infinity;
                const pb = parseFloat(b.cena) || Infinity;
                return pa - pb;
            });
            break;
        case 'price_desc':
            sorted.sort((a, b) => {
                const pa = parseFloat(a.cena) || -Infinity;
                const pb = parseFloat(b.cena) || -Infinity;
                return pb - pa;
            });
            break;
        case 'name_asc':
            sorted.sort((a, b) => a.nazwa.localeCompare(b.nazwa, 'pl'));
            break;
        case 'name_desc':
            sorted.sort((a, b) => b.nazwa.localeCompare(a.nazwa, 'pl'));
            break;
        case 'source':
            sorted.sort((a, b) => a.zrodlo.localeCompare(b.zrodlo, 'pl'));
            break;
    }

    renderTable(sorted);
}

// ===== CSV DOWNLOAD =====
async function downloadCSV() {
    if (!currentJobId) return;

    try {
        const res = await fetch(`/api/jobs/${currentJobId}`);
        const job = await res.json();

        if (!job.csv_file) {
            showToast('Plik CSV nie jest jeszcze gotowy', 'error');
            return;
        }

        window.open(`/api/exports/${job.csv_file}`, '_blank');
        showToast('Pobieranie pliku CSV...', 'success');
    } catch (err) {
        showToast('Błąd podczas pobierania', 'error');
    }
}

// ===== EXPORTS =====
async function loadExports() {
    try {
        const res = await fetch('/api/exports');
        const data = await res.json();
        const list = document.getElementById('exportsList');

        if (data.exports.length === 0) {
            list.innerHTML = `
                <div class="empty-state">
                    <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                        <path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"></path>
                        <polyline points="13 2 13 9 20 9"></polyline>
                    </svg>
                    <p>Brak plików CSV</p>
                </div>
            `;
            return;
        }

        list.innerHTML = data.exports.map(exp => `
            <div class="export-item">
                <div class="export-info">
                    <div class="export-name">${escapeHtml(exp.filename)}</div>
                    <div class="export-meta">
                        <span>${exp.size_kb} KB</span>
                        <span>${exp.created}</span>
                    </div>
                </div>
                <div class="export-actions">
                    <button class="btn-sm" onclick="window.open('/api/exports/${encodeURIComponent(exp.filename)}', '_blank')">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
                            <polyline points="7 10 12 15 17 10"></polyline>
                            <line x1="12" y1="15" x2="12" y2="3"></line>
                        </svg>
                        Pobierz
                    </button>
                    <button class="btn-sm danger" onclick="deleteExport('${escapeHtml(exp.filename)}')">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <polyline points="3 6 5 6 21 6"></polyline>
                            <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
                        </svg>
                        Usuń
                    </button>
                </div>
            </div>
        `).join('');
    } catch (err) {
        console.error('Failed to load exports:', err);
    }
}

async function deleteExport(filename) {
    try {
        const res = await fetch(`/api/exports/${encodeURIComponent(filename)}`, { method: 'DELETE' });
        if (res.ok) {
            showToast('Plik usunięty', 'success');
            loadExports();
        } else {
            showToast('Nie udało się usunąć pliku', 'error');
        }
    } catch (err) {
        showToast('Błąd podczas usuwania', 'error');
    }
}

// ===== JOBS / HISTORY =====
async function loadJobs() {
    try {
        const res = await fetch('/api/jobs');
        const data = await res.json();
        const list = document.getElementById('historyList');

        if (data.jobs.length === 0) {
            list.innerHTML = `
                <div class="empty-state">
                    <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                        <path d="M12 8v4l3 3"></path>
                        <circle cx="12" cy="12" r="10"></circle>
                    </svg>
                    <p>Brak historii wyszukiwań</p>
                </div>
            `;
            return;
        }

        list.innerHTML = data.jobs.map(job => {
            const statusColor = job.status === 'completed' ? 'var(--success)' :
                                job.status === 'failed' ? 'var(--danger)' :
                                'var(--warning)';
            const statusText = job.status === 'completed' ? 'Zakończone' :
                               job.status === 'failed' ? 'Błąd' :
                               job.status === 'running' ? 'W toku...' : 'Oczekuje';

            return `
                <div class="history-item">
                    <div class="history-info">
                        <div class="history-query">${escapeHtml(job.query)}</div>
                        <div class="history-meta">
                            <span style="color:${statusColor}">${statusText}</span>
                            <span>${job.products_found} produktów</span>
                            <span>${new Date(job.started_at).toLocaleString('pl-PL')}</span>
                        </div>
                    </div>
                    <div class="history-actions">
                        ${job.csv_file ? `
                            <button class="btn-sm" onclick="window.open('/api/exports/${encodeURIComponent(job.csv_file)}', '_blank')">
                                Pobierz CSV
                            </button>
                        ` : ''}
                    </div>
                </div>
            `;
        }).join('');
    } catch (err) {
        console.error('Failed to load jobs:', err);
    }
}

// ===== UTILITIES =====
function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = String(text);
    return div.innerHTML;
}

function showToast(message, type = 'info') {
    const container = document.getElementById('toastContainer');
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(() => toast.remove(), 4000);
}
