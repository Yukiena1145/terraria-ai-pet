/**
 * 离线冒烟测试：不需要桌宠、不需要游戏、不需要 Python。
 *
 * 用 Node 起一个假的 Terraria-Agent HTTP 服务，验证：
 *   1. defineWorld 形状正确（id/label/defaults/create/describe）
 *   2. 17 个小动作的声明与 handler 一一对应
 *   3. 状态文字、背包/快捷栏文字、失败提示、原生面板、配置页、提示词变量
 *
 * 运行：node scripts/smoke.mjs
 */
import { createServer } from 'node:http';
import assert from 'node:assert/strict';
import definition from '../src/index.ts';
import { formatStatus, formatSlots, describeOutcome } from '../src/world.ts';

const grid = Array.from({ length: 31 }, (_, y) =>
  Array.from({ length: 31 }, (_, x) => (y === 20 ? 'D' : x === 10 && y < 20 ? 'T' : '.')).join(''),
);

let lastAction = null;

const state = {
  ok: true,
  ready: true,
  error: '',
  action: 'Idle',
  busy: false,
  player: { x_tiles: 100.5, y_tiles: 40, tile_x: 100, tile_y: 40, hp: 80, max_hp: 100, vx: 0, vy: 0, on_ground: true },
  tool: { name: '铜镐', slot: 2 },
  inventory: [
    { slot: 1, name: '木材', stack: 50 },
    { slot: 2, name: '铜镐', stack: 1 },
    { slot: 3, name: '工作台', stack: 1 },
    { slot: 11, name: '火把', stack: 20 },
  ],
  enemies: [{ i: 4, x: 1600, y: 600, w: 16, h: 16, life: 20, type: 3 }],
  grid,
  grid_radius: 15,
  grid_center: [15, 15],
  logs: [{ time: '10:00:00', level: 'info', message: '砍树完成' }],
};

const server = createServer((req, res) => {
  const url = new URL(req.url, 'http://127.0.0.1');
  const send = (statusCode, body) => {
    res.writeHead(statusCode, { 'Content-Type': 'application/json; charset=utf-8' });
    res.end(JSON.stringify(body));
  };

  if (url.pathname === '/api/health') {
    return send(200, { ok: true, busy: false, game_ready: true, armed: true });
  }
  if (url.pathname === '/api/state') {
    return send(200, state);
  }
  if (url.pathname === '/api/items') {
    return send(200, { ok: true, query: url.searchParams.get('q') ?? '', items: [{ id: 3509, name: '铜短剑' }] });
  }
  if (url.pathname === '/api/command' && req.method === 'POST') {
    let raw = '';
    req.on('data', (chunk) => (raw += chunk));
    req.on('end', () => {
      const payload = JSON.parse(raw || '{}');
      lastAction = { command: payload.command };
      return send(202, { ok: true, message: '指令已发送', reply: '收到' });
    });
    return;
  }
  if (url.pathname === '/api/action' && req.method === 'POST') {
    let raw = '';
    req.on('data', (chunk) => (raw += chunk));
    req.on('end', () => {
      const payload = JSON.parse(raw || '{}');
      lastAction = payload;
      if (payload.action === 'place_block') {
        return send(200, {
          ok: false,
          action: payload.action,
          result: { ok: false, status: 'not_placed', message: '(100,40) 还是空气' },
        });
      }
      if (payload.action === 'check_hotbar') {
        return send(200, {
          ok: true,
          action: payload.action,
          result: {
            ok: true,
            status: 'success',
            message: '快捷栏 3 格有东西',
            data: {
              slots: [
                { slot: 1, name: '木材', stack: 50 },
                { slot: 2, name: '铜镐', stack: 1 },
                { slot: 3, name: '工作台', stack: 1 },
              ],
              empty: [4, 5, 6, 7, 8, 9, 10],
              selected: 2,
            },
          },
        });
      }
      if (payload.action === 'find_hotbar') {
        return send(200, {
          ok: true,
          action: payload.action,
          result: { ok: true, status: 'success', message: '「工作台」在快捷栏 slot 3', data: { slot: 3, name: '工作台', selected: 2 } },
        });
      }
      if (payload.action === 'check_inventory') {
        return send(200, {
          ok: true,
          action: payload.action,
          result: {
            ok: true,
            status: 'success',
            message: '主背包 1 格有东西',
            data: { slots: [{ slot: 11, name: '火把', stack: 20 }], empty: [12, 13, 14] },
          },
        });
      }
      if (payload.action === 'chop_tree') {
        return send(200, {
          ok: false,
          action: payload.action,
          result: {
            ok: false,
            status: 'too_far',
            message: '最近的树在右边 9 格、上下 1 格，超出 4 格砍伐范围',
            data: { dx: 9, dy: -1 },
          },
        });
      }
      return send(200, {
        ok: true,
        action: payload.action,
        result: { ok: true, status: 'success', message: `执行了 ${payload.action}` },
      });
    });
    return;
  }
  return send(404, { ok: false, error: 'not found' });
});

await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
const port = server.address().port;
const baseUrl = `http://127.0.0.1:${port}`;

assert.equal(definition.id, 'terraria');
assert.equal(typeof definition.label, 'string');
assert.equal(typeof definition.defaults, 'function');
assert.equal(typeof definition.create, 'function');

const world = definition.create({ cfg: { panelUrl: baseUrl, autoStart: false, showMap: true }, dataDir: '' });
assert.equal(world.id, 'terraria');

const tools = world.tools();
const names = tools.map((tool) => tool.name);
assert.equal(tools.length, 26, `期望 26 个工具，实际 ${tools.length}`);
for (const tool of tools) {
  assert.equal(typeof tool.handler, 'function', `${tool.name} 缺 handler`);
  assert.ok(tool.description.length > 10, `${tool.name} description 太短`);
}
assert.deepEqual(
  names.sort(),
  [
    'terraria_see', 'terraria_move', 'terraria_jump', 'terraria_jump_onto', 'terraria_chop_tree', 'terraria_mine',
    'terraria_place', 'terraria_build_pillar', 'terraria_build_frame', 'terraria_build_blueprint', 'terraria_place_wall', 'terraria_place_wall_area', 'terraria_check_house', 'terraria_open_inventory', 'terraria_close_inventory',
    'terraria_check_hotbar', 'terraria_check_inventory', 'terraria_find_in_hotbar', 'terraria_move_item',
    'terraria_select_slot', 'terraria_craft', 'terraria_fight', 'terraria_hold_fire', 'terraria_stop',
    'terraria_lookup_item', 'terraria_launch_game',
  ].sort(),
);

// describe 的工具名必须和 tools() 一致，否则桌宠日志会报未声明。
const described = definition.describe.tools.map((tool) => tool.name).sort();
assert.deepEqual(described, [...names].sort());

const byName = Object.fromEntries(tools.map((tool) => [tool.name, tool]));
const asText = (value) => (typeof value === 'string' ? value : value.text);

// 1) 看状态
const seen = asText(await byName.terraria_see.handler({}));
assert.ok(seen.includes('游戏已就绪'), seen);
assert.ok(seen.includes('格(100,40)'), seen);
assert.ok(seen.includes('★'), seen);
assert.ok(seen.includes('木材×50'), seen);
assert.ok(seen.includes('敌人(1/1') && seen.includes('150格内'), seen);

// 2) 走路
const moved = asText(await byName.terraria_move.handler({ direction: 'right', distance: 4 }));
assert.ok(moved.includes('向右走 4 格'), moved);
assert.deepEqual(lastAction, { action: 'move', params: { direction: 'right', distance: 4 } });

// 3) 砍树太远：应该给出“先走”的提示
const chopped = await byName.terraria_chop_tree.handler({});
assert.equal(chopped.failed, true, chopped);
assert.ok(chopped.text.includes('too_far'), chopped.text);
assert.ok(chopped.text.includes('terraria_move'), chopped.text);
assert.deepEqual(lastAction, { action: 'chop_tree', params: { local_only: true } });

// 4) 快捷栏 / 背包
const hotbar = asText(await byName.terraria_check_hotbar.handler({}));
assert.ok(hotbar.includes('快捷栏'), hotbar);
assert.ok(hotbar.includes('1:木材×50'), hotbar);
assert.ok(hotbar.includes('当前选中：slot 2'), hotbar);
const bag = asText(await byName.terraria_check_inventory.handler({}));
assert.ok(bag.includes('主背包'), bag);
assert.ok(bag.includes('11:火把×20'), bag);

// 4b) 跳上方块
const jumped = asText(await byName.terraria_jump_onto.handler({ x: 5, y: 6 }));
assert.ok(jumped.includes('跳上 (5,6)'), jumped);

// 4c) 搭柱子
const pillar = asText(await byName.terraria_build_pillar.handler({ height: 6 }));
assert.ok(pillar.includes('搭 6 格柱子'), pillar);

// 4d) 刷墙 + 房屋检查
const wall = asText(await byName.terraria_place_wall.handler({ x: 5, y: 6 }));
assert.ok(wall.includes('刷墙 (5,6)'), wall);
const wallArea = asText(await byName.terraria_place_wall_area.handler({ x1: 1, y1: 2, x2: 3, y2: 4 }));
assert.ok(wallArea.includes('批量刷墙'), wallArea);
const house = asText(await byName.terraria_check_house.handler({}));
assert.ok(house.includes('检查房子'), house);

// 4e) 按图纸搭方框
const frame = asText(await byName.terraria_build_frame.handler({ width: 11, height: 5, pillar: 6, direction: 'right' }));
assert.ok(frame.includes('按图纸搭方框'), frame);

// 4f) 没保存蓝图时应该拒绝并提示
const bp = asText(await byName.terraria_build_blueprint.handler({}));
assert.ok(bp.includes('还没有保存蓝图') || bp.includes('按蓝图施工'), bp);

// 4g) 持续开火
const hold = asText(await byName.terraria_hold_fire.handler({ duration_s: 5, direction: 'right' }));
assert.ok(hold.includes('持续开火 5 秒'), hold);

// 5a) 在快捷栏找东西
const foundHotbar = asText(await byName.terraria_find_in_hotbar.handler({ item: '工作台' }));
assert.ok(foundHotbar.includes('slot 3'), foundHotbar);
assert.ok(foundHotbar.includes('terraria_select_slot'), foundHotbar);

// 5b) 搬物品 + 切格
const movedItem = asText(await byName.terraria_move_item.handler({ item: '工作台', to_slot: 4 }));
assert.ok(movedItem.includes('搬运完成'), movedItem);
assert.deepEqual(lastAction, { action: 'move_item', params: { to_slot: 4, item: '工作台' } });
const selected = asText(await byName.terraria_select_slot.handler({ slot: 3 }));
assert.ok(selected.includes('切换完成'), selected);

// 6) 失败回执
const placed = await byName.terraria_place.handler({ x: 100, y: 40 });
assert.equal(placed.failed, true, placed);
assert.ok(placed.text.includes('not_placed'), placed.text);
assert.ok(placed.text.includes('提示'), placed.text);
assert.deepEqual(lastAction, { action: 'place_block', params: { x: 100, y: 40 } });

// 7) 参数校验
const bad = await byName.terraria_move.handler({});
assert.equal(bad.failed, true, bad);
const badPlace = await byName.terraria_place.handler({ x: 'abc' });
assert.equal(badPlace.failed, true, badPlace);
const badSlot = await byName.terraria_select_slot.handler({ slot: 11 });
assert.equal(badSlot.failed, true, badSlot);

// 8) 物品查询
const found = asText(await byName.terraria_lookup_item.handler({ query: '铜短剑' }));
assert.ok(found.includes('铜短剑#3509'), found);

// 9) 配置页 + 原生面板 + 提示词
const ui = world.webUi('zh');
assert.equal(ui.label, '泰拉瑞亚');
assert.equal(ui.panels[0].id, 'control');
assert.equal(ui.config[0].owner, 'world:terraria');
assert.ok(ui.config[0].schema.properties['worlds.terraria.panelUrl']);
assert.equal(ui.links[0].href, baseUrl);
assert.equal(ui.promptDocs[0].role, 'envPrompt');
assert.ok(ui.promptDocs[0].path.endsWith('ENV_PROMPT.md'));

const panelData = await ui.invoke('control', 'state', []);
assert.equal(panelData.online, true, panelData);
assert.equal(panelData.ready, true);
assert.equal(panelData.player.tile_x, 100);
assert.equal(panelData.gridRadius, 15);
assert.equal(panelData.logs.length, 1);

const commandResult = await ui.invoke('control', 'command', ['去砍树']);
assert.equal(commandResult.ok, true);
assert.deepEqual(lastAction, { command: '去砍树' });

const vars = await world.envPromptVars();
assert.ok(vars['terraria.status'].includes('在线'), vars['terraria.status']);
assert.equal(vars['terraria.panel'], baseUrl);

// 10) 纯函数
const statusText = formatStatus(
  { ready: false, error: '玩家还未进入世界', player: {}, tool: {}, inventory: [], enemies: [], grid: [] },
  { showMap: true, maxChars: 4000 },
);
assert.ok(statusText.includes('未就绪'), statusText);
assert.ok(statusText.includes('terraria_launch_game'), statusText);

const slotsText = formatSlots('快捷栏', { slots: [{ slot: 1, name: '木材', stack: 50 }], empty: [2, 3] });
assert.ok(slotsText.includes('1:木材×50'), slotsText);
assert.ok(slotsText.includes('空位：2,3'), slotsText);

// 地图：植物 P、未知 ?、怪物 E、玩家 ★ 都要画出来。
const mapStatus = formatStatus(
  {
    ready: true,
    player: { tile_x: 5, tile_y: 5, hp: 100, max_hp: 100, on_ground: true },
    tool: { name: '铜镐', slot: 1 },
    inventory: [],
    enemies: [{ i: 1, x: 6 * 16, y: 5 * 16, w: 16, h: 16, life: 10 }],
    grid: ['P..', '..?', '...'],
    grid_radius: 1,
    inventory_open: false,
  },
  { showMap: true, maxChars: 4000 },
);
assert.ok(mapStatus.includes('P'), mapStatus);
assert.ok(mapStatus.includes('?'), mapStatus);
assert.ok(mapStatus.includes('E'), mapStatus);
assert.ok(mapStatus.includes('★'), mapStatus);
assert.ok(mapStatus.includes('植物'), mapStatus);
assert.ok(mapStatus.includes('怪物'), mapStatus);
assert.ok(mapStatus.includes('背包：关着'), mapStatus);

const failedText = describeOutcome('砍树', {
  ok: false,
  status: 503,
  data: { result: { ok: false, status: 'game_not_ready', message: '游戏未就绪' } },
  error: '游戏未就绪',
});
assert.equal(failedText.failed, true);
assert.ok(failedText.text.includes('terraria_launch_game'), failedText.text);

server.close();
console.log('smoke OK：26 个小动作、状态/背包文字、失败提示、原生面板、配置页、提示词变量都正常。');
