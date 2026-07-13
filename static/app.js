const USER_ID = "dashboard_user";
const SESSION_ID = "dashboard_session";
const STORAGE_KEY = "SOPHEE_API_KEY";

// DOM Elements
const modal = document.getElementById("api-modal");
const apiKeyInput = document.getElementById("api-key-input");
const saveKeyBtn = document.getElementById("save-key-btn");
const logoutBtn = document.getElementById("logout-btn");
const chatInput = document.getElementById("chat-input");
const sendBtn = document.getElementById("send-btn");
const chatHistory = document.getElementById("chat-history");

const tabBtns = document.querySelectorAll(".tab-btn");
const panels = document.querySelectorAll(".panel");

const suggestionsList = document.getElementById("suggestions-list");
const refreshSuggestionsBtn = document.getElementById("refresh-suggestions-btn");
const favoritesList = document.getElementById("favorites-list");
const refreshFavoritesBtn = document.getElementById("refresh-favorites-btn");

// Initialize Auth
let apiKey = localStorage.getItem(STORAGE_KEY);
if (!apiKey) {
    modal.classList.remove("hidden");
}

saveKeyBtn.addEventListener("click", () => {
    const key = apiKeyInput.value.trim();
    if (key) {
        apiKey = key;
        localStorage.setItem(STORAGE_KEY, apiKey);
        modal.classList.add("hidden");
        loadSidebarData();
    }
});

logoutBtn.addEventListener("click", () => {
    localStorage.removeItem(STORAGE_KEY);
    apiKey = null;
    apiKeyInput.value = "";
    modal.classList.remove("hidden");
});

// Configure Marked.js for secure rendering
marked.setOptions({
    breaks: true,
    gfm: true
});

// Tab Switching
tabBtns.forEach(btn => {
    btn.addEventListener("click", () => {
        tabBtns.forEach(b => b.classList.remove("active"));
        panels.forEach(p => p.classList.add("hidden"));
        
        btn.classList.add("active");
        if (btn.dataset.target !== "chat") {
            const panel = document.getElementById(`${btn.dataset.target}-panel`);
            if (panel) panel.classList.remove("hidden");
        }
    });
});

// Auto-resize textarea
chatInput.addEventListener("input", function() {
    this.style.height = "auto";
    this.style.height = (this.scrollHeight) + "px";
});
chatInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
    }
});
sendBtn.addEventListener("click", sendMessage);

async function sendMessage() {
    const text = chatInput.value.trim();
    if (!text || !apiKey) return;

    chatInput.value = "";
    chatInput.style.height = "auto";
    appendMessage(text, "user");

    const typingIndicator = appendTypingIndicator();
    chatHistory.scrollTop = chatHistory.scrollHeight;

    try {
        const response = await fetch("/api/chat", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-API-Key": apiKey
            },
            body: JSON.stringify({
                user_id: USER_ID,
                session_id: SESSION_ID,
                message: text
            })
        });

        const data = await response.json();
        typingIndicator.remove();

        if (response.status === 403) {
            appendMessage("Authentication failed. Please check your API key.", "system");
            localStorage.removeItem(STORAGE_KEY);
            modal.classList.remove("hidden");
            return;
        }

        if (data.status === "success") {
            appendMessage(data.response, "bot", data.artifacts);
            // Refresh sidebar quietly in case changes were made
            loadSidebarData();
        } else {
            appendMessage(`Error: ${data.message}`, "system");
        }

    } catch (err) {
        typingIndicator.remove();
        appendMessage(`Network error: ${err.message}`, "system");
    }
}

function appendMessage(text, sender, artifacts = []) {
    const msgDiv = document.createElement("div");
    msgDiv.className = `message ${sender}-msg`;

    const contentDiv = document.createElement("div");
    contentDiv.className = "msg-content";

    if (sender === "bot") {
        const rawHtml = marked.parse(text);
        const cleanHtml = DOMPurify.sanitize(rawHtml);
        
        const tempDiv = document.createElement("div");
        tempDiv.innerHTML = cleanHtml;
        const images = tempDiv.querySelectorAll("img");
        images.forEach(img => {
            const srcAttr = img.getAttribute("src");
            if (srcAttr && srcAttr.startsWith("/api/artifacts/")) {
                img.setAttribute("src", srcAttr + (srcAttr.includes("?") ? "&" : "?") + `api_key=${apiKey}`);
                img.className = "artifact-img";
            }
        });
        contentDiv.innerHTML = tempDiv.innerHTML;

        if (artifacts && artifacts.length > 0) {
            artifacts.forEach(filename => {
                const img = document.createElement("img");
                img.src = `/api/artifacts/${USER_ID}/${SESSION_ID}/${filename}?api_key=${apiKey}`;
                img.className = "artifact-img";
                img.alt = filename;
                contentDiv.appendChild(img);
            });
        }
    } else {
        contentDiv.textContent = text;
    }

    msgDiv.appendChild(contentDiv);
    chatHistory.appendChild(msgDiv);
    chatHistory.scrollTop = chatHistory.scrollHeight;
}

function appendTypingIndicator() {
    const indicator = document.createElement("div");
    indicator.className = "message bot-msg";
    indicator.innerHTML = `<div class="typing-indicator">Sophee is typing...</div>`;
    chatHistory.appendChild(indicator);
    return indicator;
}

// Sidebar Data Fetching
async function loadSidebarData() {
    if (!apiKey) return;
    
    // Fetch Suggestions
    try {
        const suggRes = await fetch("/api/suggestions", { headers: { "X-API-Key": apiKey } });
        if (suggRes.ok) {
            const data = await suggRes.json();
            if (data.status === "success") {
                renderList(suggestionsList, data.contents.split("\\n"));
            } else {
                suggestionsList.innerHTML = `<li>${data.message}</li>`;
            }
        }
    } catch(e) { console.error(e); }

    // Fetch Favorites
    try {
        const favRes = await fetch("/api/favorites", { headers: { "X-API-Key": apiKey } });
        if (favRes.ok) {
            const data = await favRes.json();
            if (data.status === "success") {
                const favs = data.favorites;
                const items = [];
                for (const user in favs) {
                    favs[user].forEach(t => items.push(`<b>${t.title}</b><br>${t.artist}`));
                }
                if (items.length > 0) {
                    suggestionsList.innerHTML = ""; // we render to fav list below
                    const html = items.map(i => `<li>${i}</li>`).join("");
                    favoritesList.innerHTML = html;
                } else {
                    favoritesList.innerHTML = `<li>No favorites yet.</li>`;
                }
            }
        }
    } catch(e) { console.error(e); }
}

function renderList(ulElement, lines) {
    if (!lines || lines.length === 0 || (lines.length===1 && !lines[0])) {
        ulElement.innerHTML = `<li>Empty</li>`;
        return;
    }
    const html = lines.filter(l => l.trim()).map(line => {
        // Very basic parsing for suggestion list: - [x] **[date]** ...
        const clean = DOMPurify.sanitize(marked.parseInline(line));
        return `<li>${clean}</li>`;
    }).join("");
    ulElement.innerHTML = html;
}

refreshSuggestionsBtn.addEventListener("click", loadSidebarData);
refreshFavoritesBtn.addEventListener("click", loadSidebarData);

// Initial Load
if (apiKey) {
    loadSidebarData();
}
