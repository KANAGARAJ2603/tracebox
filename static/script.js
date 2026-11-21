/* TraceBox front end: sends code to /trace, then plays the returned frames. */

const PRESETS = {
    loop: `numbers = [10, 25, 30, 15]
total = 0

for n in numbers:
    if n > 15:
        total = total + n

print("Filtered Sum:", total)`,
    list: `scores = [72, 88, 95, 60]
passed = []

for s in scores:
    if s >= 75:
        passed.append(s)

print("Passed count:", len(passed))`,
    bubble: `arr = [5, 2, 8, 1]
n = len(arr)

for i in range(n - 1):
    if arr[i] > arr[i + 1]:
        arr[i], arr[i + 1] = arr[i + 1], arr[i]

print("After 1st Pass:", arr)`,
    factorial: `def factorial(n):
    if n <= 1:
        return 1
    return n * factorial(n - 1)

result = factorial(4)
print("4! =", result)`
};

let traceSteps = [];
let currentStepIdx = 0;
let sourceLines = [];
let traceError = null;

const codeEditor = document.getElementById("codeEditor");
const codeTraceView = document.getElementById("codeTraceView");
const visualizeBtn = document.getElementById("visualizeBtn");
const editModeBtn = document.getElementById("editModeBtn");
const presetSelect = document.getElementById("presetSelect");
const memoryGrid = document.getElementById("memoryGrid");
const stdoutBox = document.getElementById("stdoutBox");
const stepCounter = document.getElementById("stepCounter");
const activeLineInfo = document.getElementById("activeLineInfo");
const errorBanner = document.getElementById("errorBanner");
const firstBtn = document.getElementById("firstBtn");
const prevBtn = document.getElementById("prevBtn");
const nextBtn = document.getElementById("nextBtn");
const lastBtn = document.getElementById("lastBtn");

/* ---------- Small helpers ---------- */
function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;   // textContent => no HTML injection
    return node;
}

function showBanner(message, kind) {
    errorBanner.textContent = message;
    errorBanner.className = "banner " + kind;
}

function hideBanner() {
    errorBanner.classList.add("hidden");
}

function setStepButtonsEnabled(enabled) {
    firstBtn.disabled = !enabled;
    prevBtn.disabled = !enabled;
    nextBtn.disabled = !enabled;
    lastBtn.disabled = !enabled;
}

/* ---------- Mode switching ---------- */
function switchToEditMode() {
    codeEditor.classList.remove("hidden");
    codeTraceView.classList.add("hidden");
    editModeBtn.classList.add("hidden");
    hideBanner();
    setStepButtonsEnabled(false);
    traceSteps = [];
    stepCounter.textContent = "Step 0 / 0";
    activeLineInfo.textContent = "Editing";
    memoryGrid.replaceChildren(el("p", "empty", "Click Visualize Execution to see variables here."));
    stdoutBox.textContent = "";
}

function switchToTraceMode() {
    codeEditor.classList.add("hidden");
    codeTraceView.classList.remove("hidden");
    editModeBtn.classList.remove("hidden");
}

presetSelect.addEventListener("change", () => {
    codeEditor.value = PRESETS[presetSelect.value];
    switchToEditMode();
});
editModeBtn.addEventListener("click", switchToEditMode);

/* Let the Tab key indent instead of leaving the textarea. */
codeEditor.addEventListener("keydown", (event) => {
    if (event.key === "Tab") {
        event.preventDefault();
        const { selectionStart: start, selectionEnd: end } = codeEditor;
        codeEditor.setRangeText("    ", start, end, "end");
    }
});

/* ---------- Rendering ---------- */
function renderCodeViewer(source) {
    sourceLines = source.split("\n");
    const rows = sourceLines.map((text, i) => {
        const row = el("div", "code-line");
        row.dataset.line = String(i + 1);
        row.appendChild(el("span", "ln", String(i + 1)));
        row.appendChild(el("span", "code", text === "" ? " " : text));
        return row;
    });
    codeTraceView.replaceChildren(...rows);
}

function buildValueView(info) {
    if (info.items) {
        const wrap = el("div");
        const cells = el("div", "cells");
        info.items.forEach((value, i) => {
            const cell = el("div", "cell");
            cell.appendChild(el("div", "idx", String(i)));
            cell.appendChild(el("div", "val", value));
            cells.appendChild(cell);
        });
        wrap.appendChild(cells);
        const meta = info.size > info.items.length
            ? `${info.size} items (showing ${info.items.length})`
            : `${info.size} item${info.size === 1 ? "" : "s"}`;
        wrap.appendChild(el("div", "var-meta", meta));
        return wrap;
    }
    if (info.entries) {
        const wrap = el("div");
        const table = el("table", "entries");
        info.entries.forEach(([key, value]) => {
            const row = el("tr");
            row.appendChild(el("td", "", key));
            row.appendChild(el("td", "", value));
            table.appendChild(row);
        });
        wrap.appendChild(table);
        wrap.appendChild(el("div", "var-meta", `${info.size} key${info.size === 1 ? "" : "s"}`));
        return wrap;
    }
    return el("div", "var-scalar", info.repr);
}

function renderMemory(step, previous) {
    const names = Object.keys(step.variables);
    if (names.length === 0) {
        memoryGrid.replaceChildren(el("p", "empty", "No variables yet in this scope."));
        return;
    }
    const cards = names.map((name) => {
        const info = step.variables[name];
        const before = previous ? previous.variables[name] : undefined;
        const changed = previous && JSON.stringify(before) !== JSON.stringify(info);

        const card = el("div", "var-card" + (changed ? " changed" : ""));
        const head = el("div", "var-head");
        head.appendChild(el("span", "var-name", name));
        head.appendChild(el("span", "var-type", info.type));
        card.appendChild(head);
        card.appendChild(buildValueView(info));
        return card;
    });
    memoryGrid.replaceChildren(...cards);
}

function renderStep(idx) {
    if (traceSteps.length === 0) return;
    currentStepIdx = Math.max(0, Math.min(idx, traceSteps.length - 1));
    const step = traceSteps[currentStepIdx];
    const previous = currentStepIdx > 0 ? traceSteps[currentStepIdx - 1] : null;
    const isLast = currentStepIdx === traceSteps.length - 1;

    // Line highlighting
    codeTraceView.querySelectorAll(".code-line").forEach((row) => {
        const lineNo = Number(row.dataset.line);
        row.classList.toggle("active", lineNo === step.line);
        row.classList.toggle("error-line", Boolean(traceError && isLast && traceError.line === lineNo));
    });
    const activeRow = codeTraceView.querySelector(".code-line.active");
    if (activeRow) activeRow.scrollIntoView({ block: "nearest" });

    // Counters, buttons, status line
    stepCounter.textContent = `Step ${currentStepIdx + 1} / ${traceSteps.length}`;
    firstBtn.disabled = prevBtn.disabled = currentStepIdx === 0;
    nextBtn.disabled = lastBtn.disabled = isLast;

    let status = `Line ${step.line} · ${step.func}()`;
    if (step.event === "return") status += ` · returns ${step.return_value.repr}`;
    if (step.event === "end") status += " · finished";
    if (step.depth > 1) status += ` · call depth ${step.depth}`;
    activeLineInfo.textContent = status;

    renderMemory(step, previous);
    stdoutBox.textContent = step.stdout;
}

/* ---------- Talking to the backend ---------- */
visualizeBtn.addEventListener("click", async () => {
    const code = codeEditor.classList.contains("hidden")
        ? sourceLines.join("\n")
        : codeEditor.value;

    visualizeBtn.disabled = true;
    visualizeBtn.textContent = "Running…";
    hideBanner();

    try {
        const response = await fetch("/trace", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ code })
        });
        const data = await response.json();

        traceSteps = data.steps || [];
        traceError = data.error || null;

        if (traceSteps.length === 0) {
            // Nothing to step through: syntax / security / timeout error.
            switchToEditMode();
            const err = data.error || { type: "Error", message: "Unknown error." };
            const where = err.line ? ` (line ${err.line})` : "";
            showBanner(`${err.type}${where}: ${err.message}`, "error");
            return;
        }

        renderCodeViewer(code);
        switchToTraceMode();
        renderStep(0);

        if (data.truncated && data.message) {
            showBanner(data.message, "warn");
        } else if (traceError) {
            const where = traceError.line ? ` (line ${traceError.line})` : "";
            showBanner(`${traceError.type}${where}: ${traceError.message}`, "error");
        }
    } catch (networkError) {
        showBanner("Could not reach the TraceBox server. Is it running?", "error");
    } finally {
        visualizeBtn.disabled = false;
        visualizeBtn.textContent = "Visualize Execution";
    }
});

/* ---------- Step controls ---------- */
firstBtn.addEventListener("click", () => renderStep(0));
prevBtn.addEventListener("click", () => renderStep(currentStepIdx - 1));
nextBtn.addEventListener("click", () => renderStep(currentStepIdx + 1));
lastBtn.addEventListener("click", () => renderStep(traceSteps.length - 1));

document.addEventListener("keydown", (event) => {
    if (traceSteps.length === 0 || event.target === codeEditor) return;
    if (event.key === "ArrowRight") renderStep(currentStepIdx + 1);
    if (event.key === "ArrowLeft") renderStep(currentStepIdx - 1);
});
