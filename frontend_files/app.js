document.addEventListener('DOMContentLoaded', () => {
    
    // --- NEW TAB SWITCHING LOGIC ---
    const tabButtons = document.querySelectorAll('.tab-btn');
    const tabContents = document.querySelectorAll('.tab-content');

    const params = new URLSearchParams(window.location.search);
    const data = params.get('playerName');
    if (data) {
        localStorage.setItem('lastPlayerQuery', data);
    }
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
    let playerSearcher = null; // For player name autocomplete
    let mapSearcher = null; // For map name autocomplete
    
    // --- Result Area Elements ---
    const playerResultsEl = document.getElementById('player-results');
    const playerAutocompleteEl = document.getElementById('player-autocomplete');
    const playerSearchInput = document.getElementById('player-search');
    
    const ocrResultsEl = document.getElementById('ocr-results');
    const ocrSearchInput = document.getElementById('ocr-search');
    
    const mapResultsEl = document.getElementById('map-results');
    const mapAutocompleteEl = document.getElementById('map-autocomplete');
    const mapSearchInput = document.getElementById('map-search');
    
    const loadingEl = document.getElementById('loading');
    
    function updateBarStatsLink(playerName) {
        const link = document.getElementById('barstats-link');
        if (!link) return;

        const baseUrl = "http://bar-stats.pro/playerstats";
        
        if (playerName && playerName.trim().length > 0) {
            // Encode the name to handle spaces and special characters safely
            link.href = `${baseUrl}?playerName=${encodeURIComponent(playerName.trim())}`;
        } else {
            // Reset to base URL if input is empty
            link.href = baseUrl;
        }
    }
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

            // 7. Initialize Fuse.js instances
            loadingEl.innerText = 'Indexing data...';
            
            // OCR Searcher (existing)
            const ocrOptions = {
                keys: ['ocr_name'],
                includeScore: true,
                threshold: 0.4,
            };
            ocrSearcher = new Fuse(allData.ocr_index, ocrOptions);
            
            // Player Autocomplete Searcher (New)
            const playerOptions = {
                includeScore: true,
                threshold: 0.3, // A bit stricter for autocomplete
            };
            playerSearcher = new Fuse(allData.all_player_names, playerOptions);

            // Map Autocomplete Searcher (New)
            const mapOptions = {
                includeScore: true,
                threshold: 0.3,
            };
            mapSearcher = new Fuse(allData.all_map_names, mapOptions);
            
            loadingEl.innerText = 'Data Loaded!';
            console.log('Data loaded and indexed.', allData);

            // --- (FIX) ATTACH EVENT LISTENERS ---
            // Now that searchers are initialized, attach listeners.

            // OCR Search
            ocrSearchInput.addEventListener('input', (e) => {
                OCRsearchPlayer(e.target.value);
            });
            
            // Player Search (default options: minChars: 2, showOnFocus: false)
            setupAutocomplete(
                playerSearchInput,
                playerAutocompleteEl,
                playerSearcher,
                (selectedValue) => {
                    searchPlayer(selectedValue); // This is the onSelect callback
                }
            );
            
            // Map Search (NEW: custom options)
            setupAutocomplete(
                mapSearchInput,
                mapAutocompleteEl,
                mapSearcher,
                (selectedValue) => {
                    searchMap(selectedValue); // This is the onSelect callback
                },
                {
                    minChars: 0,
                    showOnFocus: true,
                    allItems: allData.all_map_names
                }
            );
            // --- END EVENT LISTENERS ---

            // Restore last searches
            if (localStorage.getItem('lastPlayerQuery') && playerResultsEl) {
                playerSearchInput.value = localStorage.getItem('lastPlayerQuery');
                searchPlayer(localStorage.getItem('lastPlayerQuery'));
            };
            if (localStorage.getItem('lastMapQuery') && mapResultsEl) {
                mapSearchInput.value = localStorage.getItem('lastMapQuery');
                searchMap(localStorage.getItem('lastMapQuery'));
            };
            if (localStorage.getItem('lastPlayerOCRQuery')) {
                ocrSearchInput.value = localStorage.getItem('lastPlayerOCRQuery');
                OCRsearchPlayer(localStorage.getItem('lastPlayerOCRQuery'));
            }
            
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

    // --- Autocomplete Helper Function (MODIFIED) ---
    function setupAutocomplete(inputEl, dropdownEl, searcher, onSelect, options = {}) {
        // --- NEW: Default options ---
        const { minChars = 2, showOnFocus = false, allItems = [] } = options;
        let activeIndex = -1;

        // --- Renders fuzzy search results from Fuse.js ---
        function showResults(query) {
            if (!searcher) return; 
            
            const results = searcher.search(query, { limit: 10 });
            dropdownEl.innerHTML = '';
            
            if (results.length === 0) {
                dropdownEl.style.display = 'none';
                return;
            }

            results.forEach((result, index) => {
                const item = document.createElement('div');
                item.classList.add('autocomplete-item');
                item.innerText = result.item; // result.item is the string
                item.dataset.index = index;
                
                item.addEventListener('click', () => {
                    inputEl.value = result.item;
                    onSelect(result.item);
                    dropdownEl.style.display = 'none';
                });
                
                dropdownEl.appendChild(item);
            });
            
            dropdownEl.style.display = 'block';
            activeIndex = -1;
        }

        // --- NEW: Renders a slice of all items (for focus/empty) ---
        function showAllResults() {
            if (allItems.length === 0) return;

            dropdownEl.innerHTML = '';
            const resultsToShow = allItems.slice(0, 100); // Limit to 100
            
            if (resultsToShow.length === 0) {
                dropdownEl.style.display = 'none';
                return;
            }

            resultsToShow.forEach((itemText, index) => {
                const item = document.createElement('div');
                item.classList.add('autocomplete-item');
                item.innerText = itemText; // itemText is the string
                item.dataset.index = index;
                
                item.addEventListener('click', () => {
                    inputEl.value = itemText;
                    onSelect(itemText);
                    dropdownEl.style.display = 'none';
                });
                
                dropdownEl.appendChild(item);
            });
            
            dropdownEl.style.display = 'block';
            activeIndex = -1;
        }
        
        // --- MODIFIED: Input listener ---
        inputEl.addEventListener('input', (e) => {
            const query = e.target.value;

            // Standard behavior: if query is less than minChars, hide
            if (query.length < minChars) {
                dropdownEl.style.display = 'none';
                return;
            }

            // Custom behavior: if empty (and minChars is 0), show all
            if (query.length === 0 && minChars === 0 && showOnFocus) {
                showAllResults();
            } else {
                // Standard behavior: show fuzzy search
                showResults(query);
            }
        });

        // --- NEW: Focus listener ---
        if (showOnFocus) {
            inputEl.addEventListener('focus', () => {
                const query = inputEl.value;
                if (query.length === 0) {
                    showAllResults(); // Show all on focus if empty
                } else if (query.length >= minChars) {
                    showResults(query); // Re-show results if focusing back
                }
            });
        }

        // --- UNCHANGED: Keydown listener ---
        inputEl.addEventListener('keydown', (e) => {
            const items = dropdownEl.querySelectorAll('.autocomplete-item');
            if (items.length === 0) return;

            if (e.key === 'ArrowDown') {
                e.preventDefault();
                activeIndex = (activeIndex + 1) % items.length;
                updateActive(items);
            } else if (e.key === 'ArrowUp') {
                e.preventDefault();
                activeIndex = (activeIndex - 1 + items.length) % items.length;
                updateActive(items);
            } else if (e.key === 'Enter') {
                e.preventDefault();
                if (activeIndex > -1) {
                    items[activeIndex].click();
                } else {
                    onSelect(inputEl.value);
                    dropdownEl.style.display = 'none';
                }
            } else if (e.key === 'Escape') {
                dropdownEl.style.display = 'none';
            }
        });

        function updateActive(items) {
            items.forEach(item => item.classList.remove('active'));
            if (activeIndex > -1) {
                items[activeIndex].classList.add('active');
            }
        }
        
        // --- UNCHANGED: Click-away listener ---
        document.addEventListener('click', (e) => {
            if (!dropdownEl.contains(e.target) && e.target !== inputEl) {
                dropdownEl.style.display = 'none';
            }
        });
    }

    // --- Search 1: Player Search (Exact) ---
    function searchPlayer(query) {
        updateBarStatsLink(query);
        playerResultsEl.innerHTML = ''; // Clear previous results
        localStorage.setItem('lastPlayerQuery', query);
        if (!query || !allData.player_index) {
            return;
        }
        
        const battle_ids = allData.player_index[query] || [];
        
        if (battle_ids.length === 0) {
            playerResultsEl.innerHTML = '<p>No battles found for this exact player name.</p>';
            return;
        }
        
        let allCollectedMatches = [];

        for (const battle_id of battle_ids) {
            const matches = allData.battle_matches[battle_id] || [];
            for (const match of matches) {
                // We keep 'match' and 'battle_id' together so we can use both later
                allCollectedMatches.push({ match, battle_id });
            }
        }

        // 2. Sort the array descending (Newest dates first)
        // Uses the logic: b - a
        allCollectedMatches.sort((a, b) => {
            return b.match.upload_date - a.match.upload_date;
        });

        // 3. Generate the HTML rows from the sorted data
        let tableRows = [];

        for (const item of allCollectedMatches) {
            const match = item.match;
            const battle_id = item.battle_id;

            const ytLink = `https://www.youtube.com/watch?v=${match.video_id}`;
            const replayLink = `https://www.beyondallreason.info/replays?gameId=${battle_id}`;
            const thumbUrl = `https://i.ytimg.com/vi/${match.video_id}/mqdefault.jpg`;
            
            // Format Date
            const formattedISOString = match.upload_date.slice(0, 4) + '-' + match.upload_date.slice(4, 6) + '-' + match.upload_date.slice(6, 8);
            let date = 'N/A';
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

        if (tableRows.length === 0) {
            playerResultsEl.innerHTML = '<p>Player found in battles, but no video matches for those battles.</p>';
            return;
        }

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
    };
    
    // --- Search 2: OCR Search (Fuzzy) ---
    function OCRsearchPlayer(query) {
        ocrResultsEl.innerHTML = ''; // Clear previous results
        localStorage.setItem('lastPlayerOCRQuery', query);
        
        if (!query || !ocrSearcher) {
            if(!ocrSearcher) console.error("OCR Searcher not initialized!");
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
                    <td class="col-thumb"><a href="${ytLink}&t=${item.timestamp}s" target="_blank"><img src="${thumbUrl}" alt="Thumbnail"></a></td>
                    <td class="col-title">
                        <a href="${ytLink}&t=${item.timestamp}s" target="_blank"><strong>${item.title}</strong></a>
                        <p>Matched: ${item.ocr_name}</p>
                        <p>Uploader: ${item.uploader}</p>
                    </td>
                    <td class->${formatTimestamp(item.timestamp)}</td>
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
    };
    
    // --- Search 3: Map Search ---
    function searchMap(query) {
        mapResultsEl.innerHTML = '';
        localStorage.setItem('lastMapQuery', query);
        
        if (!query || !allData.map_index) {
            return;
        }
        
        const matches = allData.map_index[query] || [];
        
        if (matches.length === 0) {
            mapResultsEl.innerHTML = '<p>No videos found for this map.</p>';
            return;
        }
        
        let tableRows = [];
        for (const match of matches) {
            const ytLink = `https://www.youtube.com/watch?v=${match.video_id}`;
            const replayLink = `https://www.beyondallreason.info/replays?gameId=${match.battle_id}`;
            const thumbUrl = `https://i.ytimg.com/vi/${match.video_id}/mqdefault.jpg`;
            const formattedISOString = match.upload_date.slice(0, 4) + '-' + match.upload_date.slice(4, 6) + '-' + match.upload_date.slice(6, 8);
            var date = 'N/A';
            if (match.upload_date != `N/A`) {
                date = (new Date(formattedISOString)).toDateString();  
            }
            
            tableRows.push(`
                <tr>
                    <td class="col-thumb"><a href="${ytLink}&t=${match.timestamp}s" target="_blank"><img src="${thumbUrl}" alt="Thumbnail"></a></td>
                    <td class="col-title">
                        <a href="${ytLink}&t=${match.timestamp}s" target="_blank"><strong>${match.title}</strong></a>
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
        mapResultsEl.innerHTML = tableHtml;
    };
    
    // --- Start the app ---
    loadData();
});