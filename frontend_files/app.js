document.addEventListener('DOMContentLoaded', () => {
    
    // --- NEW TAB SWITCHING LOGIC ---
    const tabButtons = document.querySelectorAll('.tab-btn');
    const tabContents = document.querySelectorAll('.tab-content');

    tabButtons.forEach(button => {
        button.addEventListener('click', () => {
            // Get the target tab ID from the button's data attribute
            const targetTab = button.getAttribute('data-tab');

            // Deactivate all tabs and buttons
            tabButtons.forEach(btn => btn.classList.remove('active'));
            tabContents.forEach(content => content.classList.remove('active'));

            // Activate the clicked tab and its content
            button.classList.add('active');
            document.getElementById(targetTab).classList.add('active');
        });
    });
    // --- END NEW TAB LOGIC ---

    
    // --- Global Data Stores ---
    let allData = {};
    let ocrSearcher = null; // This will be our Fuse.js instance
    
    // --- Result Area Elements ---
    const playerResultsEl = document.getElementById('player-results');
    const ocrResultsEl = document.getElementById('ocr-results');
    const loadingEl = document.getElementById('loading');

    // --- Main Data Loading Function ---
    async function loadData() {
        loadingEl.style.display = 'block';
        loadingEl.innerText = 'Connecting...';

        try {
            // 1. Start the fetch request
            const response = await fetch('frontend_files/frontend_data.json');

            if (!response.ok) {
                throw new Error(`HTTP error! Status: ${response.status}`);
            }

            // 2. Get total size from headers
            const contentLength = response.headers.get('Content-Length');
            if (!contentLength) {
                console.warn("Could not get Content-Length. Progress bar will not be shown.");
                loadingEl.innerText = 'Loading data (size unknown)...';
                allData = await response.json(); // Fallback to simple load
            } else {
                // 3. Setup for reading the stream
                const totalSize = parseInt(contentLength, 10);
                let receivedLength = 0;
                let chunks = []; // Array to store Uin8Array chunks
                const reader = response.body.getReader();

                // 4. The read loop
                while (true) {
                    const { done, value } = await reader.read();

                    if (done) {
                        break;
                    }

                    chunks.push(value);
                    receivedLength += value.length;

                    // 5. Update UI
                    const percent = Math.round((receivedLength / totalSize) * 100);
                    loadingEl.innerText = `Loading Data... ${percent}%`;
                    loadingEl.style.setProperty('--progress', `${percent}%`);
                }

                // 6. Assemble and decode all chunks
                loadingEl.innerText = 'Parsing data...';
                let allChunks = new Uint8Array(receivedLength);
                let position = 0;
                for (let chunk of chunks) {
                    allChunks.set(chunk, position);
                    position += chunk.length;
                }

                const jsonString = new TextDecoder("utf-8").decode(allChunks);
                allData = JSON.parse(jsonString);
            }

            // 7. Initialize Fuse.js for OCR search (as before)
            loadingEl.innerText = 'Indexing data...';
            const options = {
                keys: ['ocr_name'],
                includeScore: true,
                threshold: 0.4,
            };
            ocrSearcher = new Fuse(allData.ocr_index, options);
            
            loadingEl.innerText = 'Data Loaded!';
            console.log('Data loaded and indexed.', allData);

        } catch (error) {
            loadingEl.innerText = 'Error Loading Data!';
            loadingEl.style.backgroundColor = 'red';
            console.error('Failed to load frontend data:', error);
            playerResultsEl.innerHTML = '<p>Error: Could not load data. See console.</p>';
        } finally {
            setTimeout(() => { loadingEl.style.display = 'none'; }, 2000);
        }
    }

    // --- Helper: Format timestamp (seconds to MM:SS) ---
    function formatTimestamp(seconds) {
        const min = Math.floor(seconds / 60);
        const sec = seconds % 60;
        return `${min}:${sec.toString().padStart(2, '0')}`;
    }

    // --- Search 1: Player Search (Exact) ---
    document.getElementById('player-search').addEventListener('input', (e) => {
        const query = e.target.value;
        playerResultsEl.innerHTML = ''; // Clear previous results
        
        if (!query || !allData.player_index) {
            return;
        }
        
        const battle_ids = allData.player_index[query] || [];
        
        if (battle_ids.length === 0) {
            playerResultsEl.innerHTML = '<p>No battles found for this exact player name.</p>';
            return;
        }
        
        let tableRows = [];
        for (const battle_id of battle_ids) {
            const matches = allData.battle_matches[battle_id] || [];
            
            for (const match of matches) {
                // 'match' now contains: video_id, timestamp, title, upload_date
                // This loop runs once per (battle, video) pair
                
                const ytLink = `https://www.youtube.com/watch?v=${match.video_id}`;
                const replayLink = `https://www.beyondallreason.info/replays?gameId=${battle_id}`;
                const thumbUrl = `https://i.ytimg.com/vi/${match.video_id}/mqdefault.jpg`;
                const formattedISOString = match.upload_date.slice(0, 4) + '-' + match.upload_date.slice(4, 6) + '-' + match.upload_date.slice(6, 8);
                var date = 'N/A';
                if (match.upload_date != `N/A`) {
                    date = (new Date(formattedISOString)).toDateString();  
                }
                tableRows.push(`
                    <tr>
                        <td class="col-thumb"><a href="${ytLink}" target="_blank"><img src="${thumbUrl}" href="${ytLink}" alt="Thumbnail"></a></td>
                        <td class="col-title">
                            <a href="${ytLink}" target="_blank"><strong>${match.title}</strong></a>
                            <p>Found at: ${formatTimestamp(match.timestamp)}</p>
                            <p>Uploader: ${match.uploader}</p>
                        </td>
                        <td class="col-date">${date}</td>
                        <td class="col-links">
                            <a href="${replayLink}" target="_blank" class="btn-link">Replay</a>
                            <a href="${ytLink}&t=${match.timestamp}s" target="_blank" class="btn-link">Video</a>
                        </td>
                    </tr>
                `);
            }
        }

        if (tableRows.length === 0) {
            playerResultsEl.innerHTML = '<p>Player found in battles, but no video matches for those battles.</p>';
            return;
        }

        // Wrap rows in table structure
        const tableHtml = `
            <table class="results-table">
                <thead>
                    <tr>
                        <th>Thumbnail</th>
                        <th>Video</th>
                        <th>Upload Date</th>
                        <th>Links</th>
                    </tr>
                </thead>
                <tbody>
                    ${tableRows.join('')}
                </tbody>
            </table>
        `;
        playerResultsEl.innerHTML = tableHtml;
    });
    
    // --- Search 2: OCR Search (Fuzzy) ---
    document.getElementById('ocr-search').addEventListener('input', (e) => {
        const query = e.target.value;
        ocrResultsEl.innerHTML = ''; // Clear previous results
        
        if (!query || !ocrSearcher) {
            return;
        }
        
        const results = ocrSearcher.search(query, { limit: 20 });
        
        if (results.length === 0) {
            ocrResultsEl.innerHTML = '<p>No fuzzy matches found in OCR data.</p>';
            return;
        }
        
        const tableRows = results.map(result => {
            const item = result.item;
            const ytLink = `https://www.youtube.com/watch?v=${item.video_id}`;
            const thumbUrl = `https://i.ytimg.com/vi/${item.video_id}/mqdefault.jpg`;
            
            return `
                <tr>
                    <td class="col-thumb"><img src="${thumbUrl}" alt="Thumbnail"></td>
                    <td class="col-title">
                        <strong>${item.title}</strong>
                        <p>Matched: ${item.ocr_name}</p>
                    </td>
                    <td class->${formatTimestamp(item.timestamp)}</td>
                    <td class->Uploader: ${item.uploader}</td>
                    <td class="col-links">
                        <a href="${ytLink}&t=${item.timestamp}s" target="_blank" class="btn-link">Video</a>
                    </td>
                </tr>
            `;
        }).join('');

        const tableHtml = `
            <table class="results-table">
                <thead>
                    <tr>
                        <th>Thumbnail</th>
                        <th>Video</th>
                        <th>Timestamp</th>
                        <th>Link</th>
                    </tr>
                </thead>
                <tbody>
                    ${tableRows}
                </tbody>
            </table>
        `;
        ocrResultsEl.innerHTML = tableHtml;
    });

    // --- Start the app ---
    loadData();
});