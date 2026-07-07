// Auto Movie Director — per-scene editor rows on the Movie Planner node.
// Each scene gets its own prompt box (empty = the AI writes it) with a storyboard
// thumbnail that fills in after a "1) storyboard preview" run.
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const ROW_PREFIX = "scene_row_";

function findWidget(node, name) {
    return node.widgets?.find((w) => w.name === name);
}

function overridesLines(node) {
    const w = findWidget(node, "scene_overrides");
    return (w?.value || "").split("\n");
}

function syncOverrides(node) {
    const w = findWidget(node, "scene_overrides");
    if (!w) return;
    const rows = node._amdRows || [];
    w.value = rows.map((r) => (r.textarea.value || "").replace(/\r?\n/g, " ").trim()).join("\n");
}

function makeRow(node, i, initial) {
    const wrap = document.createElement("div");
    wrap.style.cssText =
        "display:flex;gap:6px;align-items:stretch;width:100%;height:100%;" +
        "background:#26262e;border:1px solid #3a3a46;border-radius:6px;padding:5px;box-sizing:border-box;";

    const img = document.createElement("img");
    img.style.cssText =
        "width:118px;min-width:118px;height:100%;object-fit:cover;border-radius:4px;" +
        "background:#16161c;display:block;";
    img.alt = "";

    const label = document.createElement("div");
    label.textContent = `SCENE ${i + 1}`;
    label.style.cssText = "position:absolute;margin:2px 0 0 4px;font:bold 10px sans-serif;color:#ffaa28;" +
        "background:rgba(20,20,25,.75);padding:1px 5px;border-radius:3px;pointer-events:none;";

    const ta = document.createElement("textarea");
    ta.value = initial || "";
    ta.placeholder = `Scene ${i + 1} — leave empty and the AI writes it from the global prompt`;
    ta.style.cssText =
        "flex:1;resize:none;background:#1b1b22;color:#ddd;border:1px solid #33333e;border-radius:4px;" +
        "font:12px monospace;padding:6px;outline:none;min-height:0;";
    ta.addEventListener("input", () => syncOverrides(node));
    ta.addEventListener("pointerdown", (e) => e.stopPropagation());

    const imgBox = document.createElement("div");
    imgBox.style.cssText = "position:relative;display:flex;";
    imgBox.append(img, label);
    wrap.append(imgBox, ta);
    return { wrap, img, textarea: ta };
}

const ROW_H = 84;
const ROW_TOTAL = ROW_H + 4;

function rebuildRows(node) {
    const n = Math.max(1, Math.round(findWidget(node, "num_scenes")?.value || 1));
    const lines = overridesLines(node);
    const prevCount = node._amdRows ? node._amdRows.length : null;
    // remove old row widgets
    if (node._amdRows) {
        for (const r of node._amdRows) {
            const idx = node.widgets.indexOf(r.widget);
            if (idx >= 0) node.widgets.splice(idx, 1);
            r.widget.onRemove?.();
        }
    }
    node._amdRows = [];
    for (let i = 0; i < n; i++) {
        const row = makeRow(node, i, (lines[i] || "").trim());
        const widget = node.addDOMWidget(ROW_PREFIX + i, "div", row.wrap, {
            serialize: false,
            getMinHeight: () => ROW_H,
            getMaxHeight: () => ROW_H,
            getHeight: () => ROW_H,
        });
        if (widget) {
            widget.serializeValue = () => undefined;
            widget.computeSize = (w) => [w, ROW_H];
        }
        row.widget = widget;
        node._amdRows.push(row);
    }
    syncOverrides(node);
    if (prevCount === null) {
        // first layout of the node: one full size computation, and a comfortable width
        requestAnimationFrame(() => {
            const s = node.computeSize();
            node.setSize([Math.max(node.size[0], 560), s[1]]);
        });
    } else if (n !== prevCount) {
        // steady state: grow/shrink by exactly the rows delta so nothing else moves
        node.setSize([node.size[0], node.size[1] + (n - prevCount) * ROW_TOTAL]);
    }
    node.setDirtyCanvas(true, true);
}

function hideStockWidget(w) {
    if (!w) return;
    w.computeSize = () => [0, -4];
    w.hidden = true;
    for (const el of [w.inputEl, w.element]) {
        if (el && el.style) {
            el.style.display = "none";
            el.style.pointerEvents = "none";
            el.style.width = "0";
            el.style.height = "0";
        }
    }
}

function buildIdeaBox(node) {
    const gpW = findWidget(node, "global_prompt");
    if (!gpW || node._amdIdea) return;
    // hide the stock flexible widget completely; we render our own fixed-height box synced to it
    hideStockWidget(gpW);

    const wrap = document.createElement("div");
    wrap.style.cssText = "width:100%;height:100%;box-sizing:border-box;padding:1px;";
    const ta = document.createElement("textarea");
    ta.value = gpW.value || "";
    ta.placeholder = "Your movie idea — describe the hero physically (body plan, eyes, size) for cross-scene consistency";
    ta.style.cssText =
        "width:100%;height:100%;resize:none;box-sizing:border-box;background:#1b1b22;color:#ddd;" +
        "border:1px solid #3a3a46;border-radius:6px;font:13px monospace;padding:8px;outline:none;";
    ta.addEventListener("input", () => { gpW.value = ta.value; });
    ta.addEventListener("pointerdown", (e) => e.stopPropagation());
    wrap.append(ta);

    const w = node.addDOMWidget("movie_idea", "div", wrap, {
        serialize: false,
        getMinHeight: () => 170,
        getMaxHeight: () => 170,
        getHeight: () => 170,
    });
    if (w) {
        w.serializeValue = () => undefined;
        w.computeSize = (ww) => [ww, 170];
    }
    node._amdIdea = ta;
}

function refreshIdeaBox(node) {
    const gpW = findWidget(node, "global_prompt");
    if (node._amdIdea && gpW) node._amdIdea.value = gpW.value || "";
}

function rendererIdFor(planner) {
    const out = planner.outputs?.[0];
    if (!out?.links?.length) return null;
    for (const lid of out.links) {
        const link = planner.graph?.links?.[lid];
        if (!link) continue;
        const target = planner.graph.getNodeById(link.target_id);
        if (target?.comfyClass === "AMD_MovieRenderer") return String(target.id);
    }
    return null;
}

app.registerExtension({
    name: "AMD.SceneEditor",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "AMD_MoviePlanner") return;

        const onCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const r = onCreated?.apply(this, arguments);
            const node = this;

            hideStockWidget(findWidget(node, "scene_overrides"));
            const numW = findWidget(node, "num_scenes");
            if (numW) {
                const orig = numW.callback;
                numW.callback = function () {
                    const res = orig?.apply(this, arguments);
                    rebuildRows(node);
                    return res;
                };
            }
            setTimeout(() => {
                buildIdeaBox(node);
                rebuildRows(node);
            }, 0);
            return r;
        };

        const onConfigure = nodeType.prototype.onConfigure;
        nodeType.prototype.onConfigure = function () {
            const r = onConfigure?.apply(this, arguments);
            setTimeout(() => {
                buildIdeaBox(this);
                refreshIdeaBox(this);
                rebuildRows(this);
            }, 0);
            return r;
        };
    },
    setup() {
        api.addEventListener("executed", ({ detail }) => {
            const images = detail?.output?.images;
            if (!images?.length) return;
            const execNode = String(detail.node ?? detail.display_node ?? "");
            for (const planner of app.graph._nodes.filter((n) => n.comfyClass === "AMD_MoviePlanner")) {
                const rows = planner._amdRows || [];
                if (!rows.length || images.length !== rows.length) continue;
                const rid = rendererIdFor(planner);
                if (!rid) continue;
                if (execNode !== rid && !execNode.startsWith(rid + ".") && !execNode.startsWith(rid + ":")) continue;
                images.forEach((im, i) => {
                    if (!rows[i]) return;
                    const url = api.apiURL(
                        `/view?filename=${encodeURIComponent(im.filename)}&subfolder=${encodeURIComponent(im.subfolder || "")}&type=${im.type}&t=${Date.now()}`
                    );
                    rows[i].img.src = url;
                });
                planner.setDirtyCanvas(true, true);
            }
        });
    },
});
