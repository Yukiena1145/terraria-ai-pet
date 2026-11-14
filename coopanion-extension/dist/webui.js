/**
 * 泰拉瑞亚扩展的浏览器端面板。
 *
 * 为什么不用 iframe：桌宠设置窗口的协议层只允许访问 Core 和桌宠自己的 origin，
 * 直接嵌 http://127.0.0.1:5000 会被拒绝成空白页。所以这里用原生 DOM 画一个
 * “同款”控制面板：地图、血量、背包、日志、指令框、快捷按钮，全部走插件自己的
 * invoke 方法（服务端再去连本地 Python 服务）。
 *
 * 默认导出形状：{ panels: { control: { mount(ctx) } } }
 */

const STRINGS = {
  zh: {
    title: '泰拉瑞亚',
    loading: '连接中…',
    online: '服务在线',
    offline: '服务离线',
    notReady: '游戏未就绪',
    ready: '游戏就绪',
    start: '启动本地服务',
    refresh: '刷新',
    web: '网页版',
    map: '地图',
    hotbar: '快捷栏',
    inventory: '背包',
    logs: '日志',
    command: '输入指令，例如「去砍树」「准备家具」',
    send: '发送',
    chop: '砍树',
    left: '左走',
    right: '右走',
    jump: '跳',
    stop: '停止',
    bag: '开背包',
    bagOpen: '背包开',
    bagClosed: '背包关',
    empty: '空',
    hp: '血',
    held: '手上',
    pos: '格',
    starting: '正在拉起服务，要等十几秒…',
    error: '出错：',
    hint: '看不到数据时，先点「启动本地服务」，或确认 Terraria-Agent 正在运行。',
    sent: '指令已发送',
    done: '完成',
    failed: '失败',
    noGame: '游戏没开：先启动 Terraria 并进入世界。',
    bpTitle: '蓝图（点格子画，黑=空，亮=放方块；从下往上）',
    bpSave: '保存蓝图',
    bpClear: '清空',
    bpBuild: '按蓝图施工',
    bpEmpty: '还没画格子',
    bpSaved: '蓝图已保存',
    bpLoaded: '蓝图已载入',
    legend: '★你  T树  D土/草  S石  W木  B墙  P植物  ●怪  ?未知',
  },
  en: {
    title: 'Terraria',
    loading: 'Connecting…',
    online: 'Service online',
    offline: 'Service offline',
    notReady: 'Game not ready',
    ready: 'Game ready',
    start: 'Start local service',
    refresh: 'Refresh',
    web: 'Web panel',
    map: 'Map',
    hotbar: 'Hotbar',
    inventory: 'Backpack',
    logs: 'Logs',
    command: 'Type a command, e.g. "chop a tree"',
    send: 'Send',
    chop: 'Chop',
    left: 'Left',
    right: 'Right',
    jump: 'Jump',
    stop: 'Stop',
    bag: 'Inventory',
    bagOpen: 'bag open',
    bagClosed: 'bag closed',
    empty: 'empty',
    hp: 'HP',
    held: 'Held',
    pos: 'tile',
    starting: 'Starting the service, this takes ~15s…',
    error: 'Error: ',
    hint: 'If no data shows up, click "Start local service" or make sure Terraria-Agent is running.',
    sent: 'Command sent',
    done: 'Done',
    failed: 'Failed',
    noGame: 'The game is not running: launch Terraria and enter a world first.',
    bpTitle: 'Blueprint (click cells; dark = empty, bright = block; drawn bottom-up)',
    bpSave: 'Save blueprint',
    bpClear: 'Clear',
    bpBuild: 'Build blueprint',
    bpEmpty: 'No cells drawn yet',
    bpSaved: 'Blueprint saved',
    bpLoaded: 'Blueprint loaded',
    legend: '★you  T tree  D dirt/grass  S stone  W wood  B wall  P plant  ● enemy  ? unknown',
  },
};

const pick = (language) => (String(language || 'zh').toLowerCase().startsWith('zh') ? STRINGS.zh : STRINGS.en);

const COLORS = {
  '.': '',
  D: '#a06a3f', // 泥土 / 草地
  S: '#8b939e', // 石块
  T: '#2f7d46', // 树
  W: '#c98a4b', // 木块
  B: '#33475c', // 背景墙
  P: '#6fce6f', // 植物（绿色）
  M: '#9a6dbb', // 陨石
  '?': '#55606e', // 不认识的方块
  '★': '#ffd34d', // 你
};
const ENEMY_COLOR = '#ff5f56';

const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};

const styleText = `
.tr-app{display:flex;flex-direction:column;gap:8px;font:inherit;color:inherit;min-height:420px}
.tr-bar{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.tr-grow{flex:1}
.tr-name{font-weight:600}
.tr-muted{opacity:.72;font-size:12px}
.tr-dot{width:8px;height:8px;border-radius:50%;flex:none;background:#9aa4b2}
.tr-dot.on{background:#34d399;box-shadow:0 0 6px rgba(52,211,153,.6)}
.tr-dot.off{background:#f87171}
.tr-dot.wait{background:#f59e0b}
.tr-btn{appearance:none;border:1px solid rgba(128,128,128,.35);background:rgba(128,128,128,.08);color:inherit;border-radius:8px;padding:4px 10px;font:inherit;font-size:12px;cursor:pointer;text-decoration:none}
.tr-btn:hover{background:rgba(128,128,128,.18)}
.tr-btn:disabled{opacity:.45;cursor:default}
.tr-main{display:flex;gap:10px;flex-wrap:wrap}
.tr-mapcol{display:flex;flex-direction:column;gap:4px;flex:none;max-width:100%}
.tr-legend{font-size:11px;opacity:.65;max-width:372px}
.tr-map{flex:none;border:1px solid rgba(128,128,128,.25);border-radius:10px;background:#14181d;image-rendering:pixelated;max-width:100%}
.tr-side{flex:1;min-width:220px;display:flex;flex-direction:column;gap:6px}
.tr-line{font-size:12px;white-space:pre-wrap;word-break:break-word}
.tr-logs{font-size:12px;line-height:1.5;max-height:150px;overflow:auto;white-space:pre-wrap;opacity:.85}
.tr-cmd{display:flex;gap:6px;flex-wrap:wrap}
.tr-input{flex:1;min-width:180px;appearance:none;border:1px solid rgba(128,128,128,.35);background:rgba(0,0,0,.15);color:inherit;border-radius:8px;padding:6px 10px;font:inherit}
.tr-status{font-size:12px;min-height:16px;opacity:.85}
.tr-hint{font-size:12px;opacity:.7}
.tr-actions{display:flex;gap:6px;flex-wrap:wrap}
.tr-bp{border:1px solid rgba(128,128,128,.3);border-radius:10px;background:#101418;cursor:crosshair;touch-action:none;max-width:100%}
.tr-bpsec{display:flex;flex-direction:column;gap:6px}
`;

const bundle = {
  panels: {
    control: {
      mount(ctx) {
        const s = pick(ctx.language);
        const root = ctx.root;
        root.textContent = '';

        const style = el('style');
        style.textContent = styleText;
        root.appendChild(style);

        const app = el('div', 'tr-app');
        const bar = el('div', 'tr-bar');
        const dot = el('span', 'tr-dot wait');
        const name = el('span', 'tr-name', s.title);
        const stateText = el('span', 'tr-muted', s.loading);
        const grow = el('span', 'tr-grow');
        const refresh = el('button', 'tr-btn', s.refresh);
        refresh.type = 'button';
        const webLink = el('a', 'tr-btn', s.web);
        webLink.target = '_blank';
        webLink.rel = 'noreferrer';
        bar.append(dot, name, stateText, grow, refresh, webLink);

        const main = el('div', 'tr-main');
        const mapCol = el('div', 'tr-mapcol');
        const canvas = el('canvas', 'tr-map');
        canvas.width = 372;
        canvas.height = 372;
        const legend = el('div', 'tr-legend', s.legend);
        mapCol.append(canvas, legend);
        const side = el('div', 'tr-side');
        const playerLine = el('div', 'tr-line', '');
        const hotbarLine = el('div', 'tr-line', '');
        const bagLine = el('div', 'tr-line', '');
        const logs = el('div', 'tr-logs', '');
        side.append(playerLine, hotbarLine, bagLine, logs);
        main.append(mapCol, side);

        const actionBar = el('div', 'tr-actions');
        const status = el('div', 'tr-status', '');
        const hint = el('div', 'tr-hint', s.hint);

        const cmdRow = el('div', 'tr-cmd');
        const input = el('input', 'tr-input');
        input.placeholder = s.command;
        const send = el('button', 'tr-btn', s.send);
        send.type = 'button';
        cmdRow.append(input, send);

        // 蓝图编辑器：从下往上画，保存后桌宠就能照着造
        const bpSection = el('div', 'tr-bpsec');
        const bpTitle = el('div', 'tr-line', s.bpTitle);
        const BP_COLS = 16;
        const BP_ROWS = 10;
        const BP_CELL = 22;
        const bpCanvas = el('canvas', 'tr-bp');
        bpCanvas.width = BP_COLS * BP_CELL;
        bpCanvas.height = BP_ROWS * BP_CELL;
        let bpGrid = Array.from({ length: BP_ROWS }, () => Array(BP_COLS).fill(0));
        const bpStatus = el('span', 'tr-muted', '');
        const bpDraw = () => {
          const g = bpCanvas.getContext('2d');
          if (!g) return;
          g.fillStyle = '#101418';
          g.fillRect(0, 0, bpCanvas.width, bpCanvas.height);
          for (let row = 0; row < BP_ROWS; row += 1) {
            for (let col = 0; col < BP_COLS; col += 1) {
              const on = bpGrid[row][col] === 1;
              g.fillStyle = on ? '#c98a4b' : '#1b2027';
              g.fillRect(col * BP_CELL + 1, (BP_ROWS - 1 - row) * BP_CELL + 1, BP_CELL - 2, BP_CELL - 2);
            }
          }
          g.strokeStyle = 'rgba(128,128,128,.25)';
          g.lineWidth = 1;
          for (let col = 0; col <= BP_COLS; col += 1) {
            g.beginPath();
            g.moveTo(col * BP_CELL, 0);
            g.lineTo(col * BP_CELL, bpCanvas.height);
            g.stroke();
          }
          for (let row = 0; row <= BP_ROWS; row += 1) {
            g.beginPath();
            g.moveTo(0, row * BP_CELL);
            g.lineTo(bpCanvas.width, row * BP_CELL);
            g.stroke();
          }
        };
        let bpPainting = false;
        let bpPaintValue = 1;
        const bpCellAt = (event) => {
          const rect = bpCanvas.getBoundingClientRect();
          const col = Math.floor(((event.clientX - rect.left) / Math.max(1, rect.width)) * BP_COLS);
          const rowFromTop = Math.floor(((event.clientY - rect.top) / Math.max(1, rect.height)) * BP_ROWS);
          const row = BP_ROWS - 1 - rowFromTop;
          if (col < 0 || col >= BP_COLS || row < 0 || row >= BP_ROWS) return null;
          return { row, col };
        };
        bpCanvas.addEventListener('mousedown', (event) => {
          event.preventDefault();
          const cell = bpCellAt(event);
          if (!cell) return;
          bpPainting = true;
          bpPaintValue = event.button === 2 ? 0 : 1;
          bpGrid[cell.row][cell.col] = bpPaintValue;
          bpDraw();
        });
        bpCanvas.addEventListener('mousemove', (event) => {
          if (!bpPainting) return;
          const cell = bpCellAt(event);
          if (!cell) return;
          bpGrid[cell.row][cell.col] = bpPaintValue;
          bpDraw();
        });
        bpCanvas.addEventListener('contextmenu', (event) => event.preventDefault());
        window.addEventListener('mouseup', () => {
          bpPainting = false;
        });
        const bpRows = () => bpGrid.map((row) => row.join(''));
        const bpWrap = el('div', 'tr-actions');
        const bpSave = el('button', 'tr-btn', s.bpSave);
        bpSave.type = 'button';
        const bpClear = el('button', 'tr-btn', s.bpClear);
        bpClear.type = 'button';
        const bpBuild = el('button', 'tr-btn', s.bpBuild);
        bpBuild.type = 'button';
        bpWrap.append(bpSave, bpClear, bpBuild, bpStatus);
        bpSection.append(bpTitle, bpCanvas, bpWrap);
        const bpLoad = async () => {
          try {
            const saved = await invoke('blueprint.get', []);
            const rows = Array.isArray(saved && saved.rows) ? saved.rows : [];
            if (rows.length) {
              bpGrid = Array.from({ length: BP_ROWS }, (_, row) => {
                const src = String(rows[row] ?? '');
                return Array.from({ length: BP_COLS }, (_, col) => (src[col] === '1' ? 1 : 0));
              });
              bpStatus.textContent = s.bpLoaded;
            }
          } catch {
            // 没保存过蓝图
          }
          bpDraw();
        };
        bpSave.addEventListener('click', async () => {
          const rows = bpRows();
          if (!rows.some((row) => row.includes('1'))) {
            bpStatus.textContent = s.bpEmpty;
            return;
          }
          try {
            await invoke('blueprint.save', [rows]);
            bpStatus.textContent = s.bpSaved;
          } catch (err) {
            bpStatus.textContent = s.error + (err instanceof Error ? err.message : String(err));
          }
        });
        bpClear.addEventListener('click', () => {
          bpGrid = Array.from({ length: BP_ROWS }, () => Array(BP_COLS).fill(0));
          bpDraw();
          bpStatus.textContent = '';
        });
        bpBuild.addEventListener('click', async () => {
          const rows = bpRows();
          if (!rows.some((row) => row.includes('1'))) {
            bpStatus.textContent = s.bpEmpty;
            return;
          }
          bpBuild.disabled = true;
          bpStatus.textContent = '…';
          try {
            await invoke('blueprint.save', [rows]);
            const out = await invoke('action', ['build_blueprint', { rows, direction: 'right' }]);
            const result = out && out.result ? out.result : out;
            bpStatus.textContent = (result && result.message) || s.bpSaved;
          } catch (err) {
            bpStatus.textContent = s.error + (err instanceof Error ? err.message : String(err));
          } finally {
            bpBuild.disabled = false;
            void load();
          }
        });
        void bpLoad();

        app.append(bar, main, bpSection, actionBar, cmdRow, status, hint);
        root.appendChild(app);

        const setStatus = (text, kind = '') => {
          status.textContent = text;
          status.style.color = kind === 'bad' ? '#f87171' : kind === 'ok' ? '#34d399' : '';
        };

        const invoke = (method, args) => ctx.invoke(method, args);

        const quick = (label, action, params) => {
          const button = el('button', 'tr-btn', label);
          button.type = 'button';
          button.addEventListener('click', async () => {
            button.disabled = true;
            setStatus(`${label}…`);
            try {
              const out = await invoke('action', [action, params ?? {}]);
              const result = out && out.result ? out.result : out;
              const ok = Boolean(result && result.ok);
              setStatus(`${label}${ok ? ' ' + s.done : ' ' + s.failed}：${(result && result.message) || ''}`, ok ? 'ok' : 'bad');
            } catch (err) {
              setStatus(s.error + (err instanceof Error ? err.message : String(err)), 'bad');
            } finally {
              button.disabled = false;
              void load();
            }
          });
          actionBar.appendChild(button);
          return button;
        };

        quick(s.chop, 'chop_tree', { local_only: true });
        quick(s.left, 'move', { direction: 'left', distance: 3 });
        quick(s.right, 'move', { direction: 'right', distance: 3 });
        quick(s.jump, 'jump', { height: 1 });
        quick(s.bag, 'open_inventory', {});
        quick(s.stop, 'stop', {});

        const sendCommand = async () => {
          const text = input.value.trim();
          if (!text) return;
          send.disabled = true;
          setStatus(s.sent + '…');
          try {
            const out = await invoke('command', [text]);
            setStatus(`${s.sent}：${(out && (out.message || out.reply)) || ''}`, 'ok');
            input.value = '';
          } catch (err) {
            setStatus(s.error + (err instanceof Error ? err.message : String(err)), 'bad');
          } finally {
            send.disabled = false;
            void load();
          }
        };
        send.addEventListener('click', () => void sendCommand());
        input.addEventListener('keydown', (event) => {
          if (event.key === 'Enter') {
            event.preventDefault();
            void sendCommand();
          }
        });

        const startButton = el('button', 'tr-btn', s.start);
        startButton.type = 'button';
        startButton.hidden = true;
        startButton.addEventListener('click', async () => {
          startButton.disabled = true;
          setStatus(s.starting);
          try {
            await invoke('start', []);
            setStatus('');
          } catch (err) {
            setStatus(s.error + (err instanceof Error ? err.message : String(err)), 'bad');
          } finally {
            startButton.disabled = false;
            void load();
          }
        });
        bar.insertBefore(startButton, grow);

        const drawMap = (grid, radius, enemies, player) => {
          const context = canvas.getContext('2d');
          if (!context) return;
          context.clearRect(0, 0, canvas.width, canvas.height);
          context.fillStyle = '#14181d';
          context.fillRect(0, 0, canvas.width, canvas.height);
          if (!Array.isArray(grid) || !grid.length) return;
          const rows = grid.length;
          const cols = String(grid[0] ?? '').length;
          const cell = Math.max(4, Math.floor(Math.min(canvas.width / cols, canvas.height / rows)));
          for (let y = 0; y < rows; y += 1) {
            const row = String(grid[y] ?? '');
            for (let x = 0; x < cols; x += 1) {
              const ch = row[x] ?? '.';
              const color = COLORS[ch];
              if (color) {
                context.fillStyle = color;
                context.fillRect(x * cell, y * cell, cell - 1, cell - 1);
              }
            }
          }
          const originX = Math.floor(Number(player && player.tile_x) || 0) - radius;
          const originY = Math.floor(Number(player && player.tile_y) || 0) - radius;
          for (const enemy of Array.isArray(enemies) ? enemies : []) {
            const ex = (Number(enemy.x) + (Number(enemy.w) || 16) / 2) / 16 - originX;
            const ey = (Number(enemy.y) + (Number(enemy.h) || 16) / 2) / 16 - originY;
            if (!(ex >= 0 && ey >= 0 && ex < cols && ey < rows)) continue;
            context.fillStyle = ENEMY_COLOR;
            context.beginPath();
            context.arc((ex + 0.5) * cell, (ey + 0.5) * cell, Math.max(2.5, cell * 0.52), 0, Math.PI * 2);
            context.fill();
            const life = Number(enemy.life) || 0;
            if (life > 0) {
              context.fillStyle = 'rgba(255,255,255,.75)';
              context.fillRect(ex * cell, (ey + 1) * cell - 2, Math.min(1, life / 100) * cell, 2);
            }
          }
          if (radius >= 0 && radius < rows && radius < cols) {
            context.fillStyle = COLORS['★'];
            context.fillRect(radius * cell, radius * cell, cell - 1, cell - 1);
          }
        };

        let lastData = null;

        const render = (data) => {
          lastData = data;
          if (!data || !data.online) {
            dot.className = 'tr-dot off';
            stateText.textContent = s.offline;
            startButton.hidden = false;
            playerLine.textContent = s.hint;
            hotbarLine.textContent = '';
            bagLine.textContent = '';
            logs.textContent = '';
            drawMap([], -1, [], null);
            return;
          }
          startButton.hidden = true;
          webLink.href = data.panelUrl || 'http://127.0.0.1:5000';
          if (data.ready) {
            dot.className = 'tr-dot on';
            stateText.textContent = s.ready;
          } else {
            dot.className = 'tr-dot wait';
            stateText.textContent = s.notReady + (data.error ? `（${data.error}）` : '');
          }
          const player = data.player || {};
          const tool = data.tool || {};
          playerLine.textContent =
            `${s.pos}(${Math.floor(Number(player.tile_x) || 0)},${Math.floor(Number(player.tile_y) || 0)})` +
            `  ${s.hp} ${Math.round(Number(player.hp) || 0)}/${Math.round(Number(player.max_hp) || 100)}` +
            `  ${s.held} ${tool.name || '空手'}` +
            `  ${data.inventoryOpen ? s.bagOpen : s.bagClosed}` +
            (data.busy ? '  ⏳' : '');
          const inventory = Array.isArray(data.inventory) ? data.inventory : [];
          const hotbar = inventory.filter((item) => Number(item.slot) >= 1 && Number(item.slot) <= 10 && Number(item.stack) > 0);
          const bag = inventory.filter((item) => Number(item.slot) > 10 && Number(item.stack) > 0);
          hotbarLine.textContent = `${s.hotbar}：` + (hotbar.length
            ? hotbar.map((item) => `${item.slot}:${item.name}×${item.stack}`).join('  ')
            : s.empty);
          bagLine.textContent = `${s.inventory}：` + (bag.length
            ? bag.map((item) => `${item.slot}:${item.name}×${item.stack}`).join('  ')
            : s.empty);
          const entries = Array.isArray(data.logs) ? data.logs : [];
          logs.textContent = entries.map((entry) => `${entry.time ?? ''} ${entry.message ?? ''}`).join('\n');
          drawMap(data.grid, Number(data.gridRadius) || 15, data.enemies, player);
        };

        const load = async () => {
          try {
            const data = await invoke('state', []);
            render(data);
          } catch (err) {
            dot.className = 'tr-dot off';
            stateText.textContent = s.error + (err instanceof Error ? err.message : String(err));
            render({ online: false });
          }
        };

        refresh.addEventListener('click', () => void load());
        ctx.interval(() => void load(), 2000);
        void load();

        return { dispose() {} };
      },
    },
  },
};

export { bundle as default };
