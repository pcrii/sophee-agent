const STORAGE_KEY = "SOPHEE_API_KEY";

// Dynamic Session Management
let CURRENT_USER_ID = localStorage.getItem("SOPHEE_USER_ID") || "dashboard_user";
let CURRENT_SESSION_ID = localStorage.getItem("SOPHEE_SESSION_ID") || `web_${Math.random().toString(36).substr(2, 9)}`;

// Save initial if not set
localStorage.setItem("SOPHEE_USER_ID", CURRENT_USER_ID);
localStorage.setItem("SOPHEE_SESSION_ID", CURRENT_SESSION_ID);

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

const chatsList = document.getElementById("chats-list");
const newChatBtn = document.getElementById("new-chat-btn");
const refreshChatsBtn = document.getElementById("refresh-chats-btn");

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
        loadActiveChat();
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
        // Skip hidden tabs like "Active" if we are just switching via click
        if (btn.style.display === "none") return;
        
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
                user_id: CURRENT_USER_ID,
                session_id: CURRENT_SESSION_ID,
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

function appendMessage(text, sender, artifacts = [], payload = null) {
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
                img.src = `/api/artifacts/${CURRENT_USER_ID}/${CURRENT_SESSION_ID}/${filename}?api_key=${apiKey}`;
                img.className = "artifact-img";
                img.alt = filename;
                contentDiv.appendChild(img);
            });
        }
    } else {
        contentDiv.textContent = text;
    }

    msgDiv.appendChild(contentDiv);
    
    if (payload) {
        const details = document.createElement("details");
        details.className = "payload-details";
        
        const summary = document.createElement("summary");
        summary.textContent = "View ADK Payload";
        
        const pre = document.createElement("pre");
        pre.className = "payload-pre";
        pre.textContent = JSON.stringify(payload, null, 2);
        
        details.appendChild(summary);
        details.appendChild(pre);
        msgDiv.appendChild(details);
    }
    
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

// Session Loading
newChatBtn.addEventListener("click", () => {
    CURRENT_USER_ID = "dashboard_user";
    CURRENT_SESSION_ID = `web_${Math.random().toString(36).substr(2, 9)}`;
    localStorage.setItem("SOPHEE_USER_ID", CURRENT_USER_ID);
    localStorage.setItem("SOPHEE_SESSION_ID", CURRENT_SESSION_ID);
    
    chatHistory.innerHTML = `
        <div class="message system-msg">
            <div class="msg-content">
                <strong>New Chat Started.</strong><br>
                Say hello!
            </div>
        </div>
    `;
    loadSidebarData();
});

async function loadActiveChat() {
    if (!apiKey) return;
    try {
        const res = await fetch(`/api/chat/history/${CURRENT_USER_ID}/${CURRENT_SESSION_ID}`, { headers: { "X-API-Key": apiKey } });
        if (res.ok) {
            const data = await res.json();
            if (data.status === "success") {
                chatHistory.innerHTML = `
                    <div class="message system-msg">
                        <div class="msg-content">
                            <strong>Loaded Session: ${CURRENT_SESSION_ID}</strong><br>
                            User: ${CURRENT_USER_ID}
                        </div>
                    </div>
                `;
                data.history.forEach(msg => {
                    appendMessage(msg.text, msg.sender, msg.artifacts, msg.payload);
                });
            }
        }
    } catch(e) { console.error(e); }
}

function selectSession(userId, sessionId) {
    CURRENT_USER_ID = userId;
    CURRENT_SESSION_ID = sessionId;
    localStorage.setItem("SOPHEE_USER_ID", CURRENT_USER_ID);
    localStorage.setItem("SOPHEE_SESSION_ID", CURRENT_SESSION_ID);
    loadActiveChat();
}

// Sidebar Data Fetching
async function loadSidebarData() {
    if (!apiKey) return;
    
    // Fetch Sessions
    try {
        const sessRes = await fetch("/api/chat/sessions", { headers: { "X-API-Key": apiKey } });
        if (sessRes.ok) {
            const data = await sessRes.json();
            if (data.status === "success") {
                const sessions = data.sessions;
                if (sessions.length > 0) {
                    chatsList.innerHTML = "";
                    sessions.forEach(s => {
                        const li = document.createElement("li");
                        const name = s.session_id.startsWith("web_") ? "Web Chat" : "Discord Chat";
                        li.innerHTML = `<b>${name}</b><br><small style="opacity:0.6">${s.session_id}<br>${new Date(s.update_time).toLocaleDateString()}</small>`;
                        li.style.cursor = "pointer";
                        li.addEventListener("click", () => selectSession(s.user_id, s.session_id));
                        chatsList.appendChild(li);
                    });
                } else {
                    chatsList.innerHTML = `<li>No chats yet.</li>`;
                }
            }
        }
    } catch(e) { console.error(e); }

    // Fetch Suggestions
    try {
        const suggRes = await fetch("/api/suggestions", { headers: { "X-API-Key": apiKey } });
        if (suggRes.ok) {
            const data = await suggRes.json();
            if (data.status === "success") {
                renderList(suggestionsList, data.contents.split("\\n"));
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
                    favoritesList.innerHTML = items.map(i => `<li>${i}</li>`).join("");
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
        const clean = DOMPurify.sanitize(marked.parseInline(line));
        return `<li>${clean}</li>`;
    }).join("");
    ulElement.innerHTML = html;
}

refreshChatsBtn.addEventListener("click", loadSidebarData);
refreshSuggestionsBtn.addEventListener("click", loadSidebarData);
refreshFavoritesBtn.addEventListener("click", loadSidebarData);

// Initial Load
if (apiKey) {
    loadSidebarData();
    loadActiveChat();
}
