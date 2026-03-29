const state = {
    conversationId: null,
    models: [],
    conversations: [],
    streaming: false,
};

const els = {
    modelSelect: document.getElementById("model-select"),
    systemPrompt: document.getElementById("system-prompt"),
    conversationList: document.getElementById("conversation-list"),
    messages: document.getElementById("messages"),
    heroEmpty: document.getElementById("hero-empty"),
    chatTitle: document.getElementById("chat-title"),
    messageInput: document.getElementById("message-input"),
    composer: document.getElementById("composer"),
    sendBtn: document.getElementById("send-btn"),
    newChatBtn: document.getElementById("new-chat-btn"),
    deleteChatBtn: document.getElementById("delete-chat-btn"),
    healthPill: document.getElementById("health-pill"),
    messageTemplate: document.getElementById("message-template"),
};

function setBusy(isBusy) {
    state.streaming = isBusy;
    els.sendBtn.disabled = isBusy || !state.conversationId;
    els.deleteChatBtn.disabled = isBusy || !state.conversationId;
    els.modelSelect.disabled = isBusy;
}

function autoGrow(textarea) {
    textarea.style.height = "auto";
    textarea.style.height = `${Math.min(textarea.scrollHeight, 220)}px`;
}

function formatTime(value) {
    try {
        return new Date(value).toLocaleString();
    } catch {
        return value;
    }
}

function escapeHtml(value) {
    return value
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;");
}

function renderMessage(role, content) {
    const fragment = els.messageTemplate.content.cloneNode(true);
    const article = fragment.querySelector(".message");
    article.classList.add(role);
    fragment.querySelector(".message-role").textContent = role;
    fragment.querySelector(".message-body").textContent = content;
    els.messages.appendChild(fragment);
    els.messages.scrollTop = els.messages.scrollHeight;
    return els.messages.lastElementChild.querySelector(".message-body");
}

function setEmptyState() {
    els.messages.innerHTML = `
        <div class="empty-state">
            <p>Start a new chat to begin talking to your local model.</p>
        </div>
    `;
}

function renderConversationList() {
    els.conversationList.innerHTML = "";

    if (!state.conversations.length) {
        els.conversationList.innerHTML = `<p class="empty-state">No saved conversations yet.</p>`;
        return;
    }

    for (const item of state.conversations) {
        const button = document.createElement("button");
        button.className = "conversation-item";
        if (item.id === state.conversationId) {
            button.classList.add("active");
        }
        button.innerHTML = `
            <strong>${escapeHtml(item.title)}</strong>
            <span>${escapeHtml(item.model)}</span>
            <span>${escapeHtml(formatTime(item.updated_at))}</span>
        `;
        button.addEventListener("click", () => loadConversation(item.id));
        els.conversationList.appendChild(button);
    }
}

async function fetchJson(url, options = {}) {
    const response = await fetch(url, options);
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
        throw new Error(payload.detail || "Request failed");
    }
    return payload;
}

async function refreshHealth() {
    try {
        const payload = await fetchJson("/api/health");
        els.healthPill.textContent = payload.status === "ok" ? "Ollama online" : "Checking";
        els.healthPill.className = "health-pill ok";
    } catch {
        els.healthPill.textContent = "Ollama offline";
        els.healthPill.className = "health-pill offline";
    }
}

async function loadModels() {
    const payload = await fetchJson("/api/models");
    state.models = payload.models;
    els.modelSelect.innerHTML = "";

    if (!state.models.length) {
        const option = document.createElement("option");
        option.value = "";
        option.textContent = "No models found";
        els.modelSelect.appendChild(option);
        els.modelSelect.disabled = true;
        return;
    }

    for (const model of state.models) {
        const option = document.createElement("option");
        option.value = model;
        option.textContent = model;
        if (model === window.APP_CONFIG.defaultModel) {
            option.selected = true;
        }
        els.modelSelect.appendChild(option);
    }
}

async function loadConversations() {
    const payload = await fetchJson("/api/conversations");
    state.conversations = payload.conversations;
    renderConversationList();
}

async function createConversation() {
    const payload = await fetchJson("/api/conversations", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model: els.modelSelect.value || window.APP_CONFIG.defaultModel }),
    });

    state.conversationId = payload.conversation.id;
    els.chatTitle.textContent = payload.conversation.title;
    els.heroEmpty.hidden = true;
    els.messages.innerHTML = "";
    setEmptyState();
    els.deleteChatBtn.disabled = false;
    await loadConversations();
    renderConversationList();
    return payload.conversation.id;
}

async function loadConversation(id) {
    const payload = await fetchJson(`/api/conversations/${id}`);
    state.conversationId = id;
    els.chatTitle.textContent = payload.conversation.title;
    els.modelSelect.value = payload.conversation.model;
    els.heroEmpty.hidden = true;
    els.messages.innerHTML = "";

    if (!payload.messages.length) {
        setEmptyState();
    } else {
        for (const message of payload.messages) {
            renderMessage(message.role, message.content);
        }
    }

    els.deleteChatBtn.disabled = false;
    renderConversationList();
}

async function deleteCurrentConversation() {
    if (!state.conversationId) {
        return;
    }

    await fetchJson(`/api/conversations/${state.conversationId}`, { method: "DELETE" });
    state.conversationId = null;
    els.chatTitle.textContent = "New chat";
    els.heroEmpty.hidden = false;
    setEmptyState();
    els.deleteChatBtn.disabled = true;
    await loadConversations();
}

async function streamAssistantReply(content) {
    const response = await fetch(`/api/conversations/${state.conversationId}/messages`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
            content,
            model: els.modelSelect.value,
            system_prompt: els.systemPrompt.value.trim(),
        }),
    });

    if (!response.ok || !response.body) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.detail || "Streaming failed");
    }

    const assistantBody = renderMessage("assistant", "");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
        const { value, done } = await reader.read();
        if (done) {
            break;
        }

        buffer += decoder.decode(value, { stream: true });
        const events = buffer.split("\n\n");
        buffer = events.pop() || "";

        for (const rawEvent of events) {
            const lines = rawEvent.split("\n");
            const eventName = lines.find((line) => line.startsWith("event:"))?.slice(6).trim();
            const dataLine = lines.find((line) => line.startsWith("data:"))?.slice(5).trim();

            if (!eventName || !dataLine) {
                continue;
            }

            const payload = JSON.parse(dataLine);
            if (eventName === "token") {
                assistantBody.textContent += payload.content;
                els.messages.scrollTop = els.messages.scrollHeight;
            }
            if (eventName === "error") {
                throw new Error(payload.detail || "Model error");
            }
            if (eventName === "done") {
                assistantBody.textContent = payload.content;
            }
        }
    }

    await loadConversations();
    const active = state.conversations.find((item) => item.id === state.conversationId);
    if (active) {
        els.chatTitle.textContent = active.title;
    }
    renderConversationList();
}

async function handleSubmit(event) {
    event.preventDefault();
    const content = els.messageInput.value.trim();
    if (!content || state.streaming) {
        return;
    }

    if (!state.conversationId) {
        await createConversation();
    }

    if (els.messages.querySelector(".empty-state")) {
        els.messages.innerHTML = "";
    }

    renderMessage("user", content);
    els.messageInput.value = "";
    autoGrow(els.messageInput);
    setBusy(true);

    try {
        await streamAssistantReply(content);
    } catch (error) {
        renderMessage("assistant", `Error: ${error.message}`);
    } finally {
        setBusy(false);
    }
}

els.composer.addEventListener("submit", handleSubmit);
els.newChatBtn.addEventListener("click", async () => {
    await createConversation();
});
els.deleteChatBtn.addEventListener("click", deleteCurrentConversation);
els.messageInput.addEventListener("input", () => autoGrow(els.messageInput));
els.messageInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        els.composer.requestSubmit();
    }
});

(async function init() {
    setEmptyState();
    autoGrow(els.messageInput);
    await Promise.all([refreshHealth(), loadModels(), loadConversations()]);
    els.sendBtn.disabled = false;
})();
