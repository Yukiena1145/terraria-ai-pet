/**
 * 泰拉瑞亚 World：把本机 Terraria-Agent 的能力拆成一粒一粒的小动作，交给桌宠的大模型自己组合。
 *
 * 设计要点：
 * - 大脑是桌宠自己：这里不调用任何大模型，只提供“看”和“做”的小工具；
 * - 每个工具只做一件事：走路、跳、砍眼前这一棵、开关背包、看快捷栏、搬格子、制作、放置……
 * - 所有动作都走本地 Python 服务的 /api/action，插件只做参数校验和把结果翻译成人话；
 * - 服务没起来时可以自动 spawn `main.py`；
 * - 控制面板用原生 DOM 画（桌宠设置窗口会拦截外部 iframe，所以不能直接嵌网页）。
 */

import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { TerrariaAgent, type AgentActionResult, type HttpOutcome } from './agent.ts';

export const WORLD_ID = 'terraria';

/** 本扩展包根目录（src 的上一级）。 */
const PKG_DIR = join(dirname(fileURLToPath(import.meta.url)), '..');
const BLUEPRINT_PATH = join(PKG_DIR, 'blueprint.json');

/** 主人画的蓝图：rows 从下往上，'1' = 放方块。 */
let blueprintRows: string[] = [];
try {
  const saved = JSON.parse(readFileSync(BLUEPRINT_PATH, 'utf8')) as { rows?: unknown };
  if (Array.isArray(saved.rows)) blueprintRows = saved.rows.map((row) => String(row));
} catch {
  // 还没保存过蓝图
}

export interface TerrariaConfig extends Record<string, unknown> {
  enabled: boolean;
  /** 本地 Agent 面板地址，同时是插件连接地址。 */
  panelUrl: string;
  /** 运行 main.py 的 Python。 */
  pythonExe: string;
  /** Terraria-Agent 项目目录（含 main.py）。 */
  projectDir: string;
  /** 服务没起来时自动拉起。 */
  autoStart: boolean;
  /** 游戏里“背包/物品栏”的按键名（pydirectinput 键名，默认 escape）。 */
  inventoryKey: string;
  /** 单个动作最长等待（毫秒）。 */
  actionTimeoutMs: number;
  /** 状态里带不带 31×31 地形图；关掉更省 token。 */
  showMap: boolean;
  /** 状态回执最大字符数，超出截断。 */
  maxStatusChars: number;
}

export const DEFAULTS: TerrariaConfig = {
  enabled: false,
  panelUrl: 'http://127.0.0.1:5000',
  pythonExe: 'F:\\Terraria-Agent\\Terraria-Agent-V1\\.venv\\Scripts\\python.exe',
  projectDir: 'F:\\Terraria-Agent\\Terraria-Agent-V1',
  autoStart: true,
  inventoryKey: 'escape',
  actionTimeoutMs: 180_000,
  showMap: true,
  maxStatusChars: 6000,
};

interface PlayerLike {
  x_tiles?: number;
  y_tiles?: number;
  tile_x?: number;
  tile_y?: number;
  hp?: number;
  max_hp?: number;
  vx?: number;
  vy?: number;
  on_ground?: boolean;
}

interface EnemyLike {
  i?: number;
  x?: number;
  y?: number;
  w?: number;
  h?: number;
  life?: number;
  type?: number;
}

interface SlotLike {
  slot?: number;
  name?: string;
  stack?: number;
}

interface ToolSpec {
  name: string;
  description: string;
  tags: Array<'read' | 'write' | 'speak' | 'act' | 'flow' | 'snapshot'>;
  parameters: Record<string, unknown>;
}

interface ToolOutcome {
  text: string;
  failed?: true;
}

const TOOL_SUMMARIES: Record<string, string> = {
  terraria_see: '看坐标、血量、背包和 31×31 地图。',
  terraria_move: '左右走一段。',
  terraria_jump: '跳一下或边跳边走。',
  terraria_jump_onto: '跳上指定方块格。',
  terraria_chop_tree: '砍够得着的那棵树。',
  terraria_mine: '挖够得着的那块方块。',
  terraria_place: '把手上的东西放到指定格。',
  terraria_build_pillar: '在身体旁边竖着叠柱子。',
  terraria_build_frame: '按图纸搭悬空方框。',
  terraria_build_blueprint: '按主人画的蓝图施工。',
  terraria_place_wall: '刷一格背景墙。',
  terraria_place_wall_area: '批量刷背景墙。',
  terraria_check_house: '检查房子合不合格。',
  terraria_open_inventory: '打开背包。',
  terraria_close_inventory: '关闭背包。',
  terraria_check_hotbar: '看快捷栏 1-10。',
  terraria_check_inventory: '看主背包 11-50。',
  terraria_find_in_hotbar: '在快捷栏里找东西。',
  terraria_move_item: '把物品搬到别的格子。',
  terraria_select_slot: '切换快捷栏格。',
  terraria_craft: '按名字合成。',
  terraria_fight: '打最近的怪。',
  terraria_stop: '立刻停下。',
  terraria_lookup_item: '查物品名和 ID。',
  terraria_launch_game: '启动游戏。',
};

const NO_TRAVEL = '（它自己不会跑过去：不在范围内会告诉你还差几格，先 terraria_move）';

function toolSpecs(): ToolSpec[] {
  return [
    {
      name: 'terraria_see',
      description:
        '看当前状态：游戏是否就绪、你的格坐标、血量、手上工具、背包、附近敌人，以及 31×31 地形小地图（★=你，T=树，D=泥土，S=石，W=木块，B=墙，.=空气）。每次动手前先调用它，看清再决定。',
      tags: ['read', 'snapshot'],
      parameters: { type: 'object', properties: {} },
    },
    {
      name: 'terraria_move',
      description:
        '往左或往右走一段。distance 单位是格（0.5~20），闭环控制，会自己减速停稳，不会冲过头。走路是你自己的事：想去哪就分段走。',
      tags: ['act'],
      parameters: {
        type: 'object',
        properties: {
          direction: { type: 'string', enum: ['left', 'right'], description: '走的方向。' },
          distance: { type: 'number', minimum: 0.5, maximum: 20, description: '走几格，默认 3。' },
        },
        required: ['direction'],
      },
    },
    {
      name: 'terraria_jump',
      description:
        '跳一下，或者边跳边往一个方向走。height 是想起跳的高度（0~6 格，不用很精确，0 就是小跳）。',
      tags: ['act'],
      parameters: {
        type: 'object',
        properties: {
          height: { type: 'number', minimum: 0, maximum: 6, description: '跳多高（格），默认 1。' },
          direction: { type: 'string', enum: ['left', 'right'], description: '可选：边跳边往哪边走。' },
        },
      },
    },
    {
      name: 'terraria_jump_onto',
      description:
        '跳到指定的方块格顶上（先走到它旁边，再朝它蓄力跳，落点不对会自动微调，最多 3 次）。' +
        '搭高台/上自己刚放的方块时用它。x,y 是那块“方块”的格子，不是你想站的格子。',
      tags: ['act'],
      parameters: {
        type: 'object',
        properties: {
          x: { type: 'integer', description: '方块的世界格 x。' },
          y: { type: 'integer', description: '方块的世界格 y。' },
        },
        required: ['x', 'y'],
      },
    },
    {
      name: 'terraria_chop_tree',
      description:
        '砍掉视野里最近、并且够得着的那棵树（4 格内），自动切斧头、对准、按住左键。' +
        NO_TRAVEL +
        ' 太远时结果里会告诉你往哪边、还差几格。',
      tags: ['act'],
      parameters: { type: 'object', properties: {} },
    },
    {
      name: 'terraria_mine',
      description:
        '挖掉视野里最近、并且够得着的那块泥土/石块/木块（4 格内），自动切合适的镐子；' +
        '也可以传 x,y 挖指定的那一格（挡路时挖面前的方块）。' +
        NO_TRAVEL +
        ' 太远时结果里会告诉你往哪边走。',
      tags: ['act'],
      parameters: {
        type: 'object',
        properties: {
          x: { type: 'integer', description: '可选：要挖的世界格 x（和 y 一起传）。' },
          y: { type: 'integer', description: '可选：要挖的世界格 y。' },
        },
      },
    },
    {
      name: 'terraria_place',
      description:
        '把手上的东西（方块、工作台、家具）放到世界格 (x,y)。会自动找旁边的支撑方块：' +
        '放地上就点下方地面往上滑，往左放就点右边方块往左滑，往右放就点左边方块往右滑。' +
        '如果给的格子差了一两格、周围没有支撑，它会在附近两格内自动换一个能放的格子。' +
        '只放手上的，不会替你挑材料；背包必须已经关掉；目标格已经有方块时会拒绝。',
      tags: ['act'],
      parameters: {
        type: 'object',
        properties: {
          x: { type: 'integer', description: '世界格 x。' },
          y: { type: 'integer', description: '世界格 y。' },
        },
        required: ['x', 'y'],
      },
    },
    {
      name: 'terraria_build_pillar',
      description:
        '在人物身体左边或右边竖着叠 N 格（默认 6）。从地面往上叠，绝不会放在你自己站着的身体里；' +
        '回执会告诉你柱顶坐标，再用 terraria_jump_onto 跳上去。需要手上拿着方块、背包关着。',
      tags: ['act'],
      parameters: {
        type: 'object',
        properties: {
          height: { type: 'integer', minimum: 1, maximum: 8, description: '叠几格，默认 6。' },
          side: { type: 'string', enum: ['left', 'right'], description: '可选：放左边还是右边；不给就选有空位的一边。' },
        },
      },
    },
    {
      name: 'terraria_place_wall',
      description:
        '把手上的背景墙刷到世界格 (x,y)。墙不需要支撑；格子被方块占着会拒绝。' +
        '手上要先拿着木墙，背包关着。',
      tags: ['act'],
      parameters: {
        type: 'object',
        properties: {
          x: { type: 'integer', description: '世界格 x。' },
          y: { type: 'integer', description: '世界格 y。' },
        },
        required: ['x', 'y'],
      },
    },
    {
      name: 'terraria_place_wall_area',
      description:
        '在矩形区域里批量刷背景墙（最多 60 格）：从 (x1,y1) 到 (x2,y2)。逐格验证，连续失败会停。',
      tags: ['act'],
      parameters: {
        type: 'object',
        properties: {
          x1: { type: 'integer', description: '左上角 x。' },
          y1: { type: 'integer', description: '左上角 y。' },
          x2: { type: 'integer', description: '右下角 x。' },
          y2: { type: 'integer', description: '右下角 y。' },
        },
        required: ['x1', 'y1', 'x2', 'y2'],
      },
    },
    {
      name: 'terraria_check_house',
      description:
        '检查你附近的房子是不是合格的 NPC 住房：内部至少 30 格、封闭、背景墙铺满、' +
        '有门或平台、有桌子、有椅子、有光源、有实体地板。会返回缺哪几项。',
      tags: ['read', 'snapshot'],
      parameters: { type: 'object', properties: {} },
    },
    {
      name: 'terraria_build_frame',
      description:
        '按图纸搭一个悬空方框（主人指定的房子骨架）：先在地上搭 6 格柱子，跳上柱顶，' +
        '往右铺 11 格地板，右端往上 5 格，再从顶部往左 11 格回来，最后补左边一列围成方框。' +
        '需要手上拿着方块、背包关着、站在平地上。direction 可选 right（默认）或 left。',
      tags: ['act'],
      parameters: {
        type: 'object',
        properties: {
          width: { type: 'integer', minimum: 3, maximum: 20, description: '横向多少格，默认 11。' },
          height: { type: 'integer', minimum: 2, maximum: 10, description: '右端往上多少格，默认 5。' },
          pillar: { type: 'integer', minimum: 2, maximum: 6, description: '柱子高度，默认 6。' },
          direction: { type: 'string', enum: ['right', 'left'], description: '往哪边延伸，默认 right。' },
        },
      },
    },
    {
      name: 'terraria_build_blueprint',
      description:
        '严格按主人在控制面板里画好并保存的蓝图施工。蓝图底边贴着你脚下的地面，' +
        '从你身体旁边一列开始往 direction 方向铺。没保存过蓝图会拒绝，并提示主人去面板画。',
      tags: ['act'],
      parameters: {
        type: 'object',
        properties: {
          direction: { type: 'string', enum: ['right', 'left'], description: '往哪边铺，默认 right。' },
        },
      },
    },
    {
      name: 'terraria_find_in_hotbar',
      description:
        '在快捷栏 1-10 里按名字找物品，只返回它在第几格（不搬东西、不切换）。找到后可以直接 terraria_select_slot。',
      tags: ['read'],
      parameters: {
        type: 'object',
        properties: { item: { type: 'string', description: '物品中文名。' } },
        required: ['item'],
      },
    },
    {
      name: 'terraria_open_inventory',
      description: '打开背包（已经开着就不会再按 Esc）。打开后才能 check / craft / move_item。',
      tags: ['act'],
      parameters: { type: 'object', properties: {} },
    },
    {
      name: 'terraria_close_inventory',
      description: '关闭背包（已经关着就不动）。放方块、走路前要先关掉，否则鼠标会点到 UI 上。',
      tags: ['act'],
      parameters: { type: 'object', properties: {} },
    },
    {
      name: 'terraria_check_hotbar',
      description: '只看快捷栏 1-10 格：哪些格有东西、分别是什么、当前选中哪格、哪些是空的。',
      tags: ['read', 'snapshot'],
      parameters: { type: 'object', properties: {} },
    },
    {
      name: 'terraria_check_inventory',
      description: '只看主背包 11-50 格：哪些格有东西、分别是什么、有多少空位。',
      tags: ['read', 'snapshot'],
      parameters: { type: 'object', properties: {} },
    },
    {
      name: 'terraria_move_item',
      description:
        '把物品搬到另一个格子（换格子就是交换）。可以按物品名（item）找，也可以指定 from_slot。通常用来把材料搬进快捷栏。',
      tags: ['act'],
      parameters: {
        type: 'object',
        properties: {
          item: { type: 'string', description: '要搬的物品中文名，例如「工作台」。' },
          from_slot: { type: 'integer', minimum: 1, maximum: 50, description: '也可以直接指定源格子。' },
          to_slot: { type: 'integer', minimum: 1, maximum: 50, description: '搬到哪一格（必填）。' },
        },
        required: ['to_slot'],
      },
    },
    {
      name: 'terraria_select_slot',
      description: '切换快捷栏到指定格（1-10），换手上拿着的东西。',
      tags: ['act'],
      parameters: {
        type: 'object',
        properties: {
          slot: { type: 'integer', minimum: 1, maximum: 10, description: '快捷栏格号。' },
        },
        required: ['slot'],
      },
    },
    {
      name: 'terraria_craft',
      description:
        '按名字合成物品（例如「工作台」「木桌」）。要求背包已经打开（先 open_inventory）；材料不够会返回失败。' +
        'count>1 时会按住制作按钮连续制作（泰拉瑞亚支持长按连做），一次把几十上百个木墙堆起来，' +
        '不要“做 4 个放一次”。',
      tags: ['act'],
      parameters: {
        type: 'object',
        properties: {
          item: { type: 'string', description: '要制作的物品中文名。' },
          count: { type: 'integer', minimum: 1, maximum: 500, description: '做几次（默认 1）；做大量木墙/火把时填 100。' },
        },
        required: ['item'],
      },
    },
    {
      name: 'terraria_fight',
      description:
        '拿攻击力最高的武器追着最近的怪打：朝它走的同时把鼠标压在它身上按住左键挥砍（不是点一下），' +
        '持续 duration_s 秒。没武器会拒绝，不会拿镐子送。',
      tags: ['act'],
      parameters: {
        type: 'object',
        properties: {
          duration_s: { type: 'number', minimum: 0.5, maximum: 12, description: '追砍多久（秒），默认 4。' },
        },
      },
    },
    {
      name: 'terraria_hold_fire',
      description:
        '矿车/站桩持续输出：自动选最强武器，锁定最近的怪，左键按下后一直不松，' +
        '每帧把鼠标贴到怪身上（没有点击间隔、没有停顿），持续 duration_s 秒。' +
        '坐矿车打怪优先用它，不要用 terraria_fight（那个会松手）。',
      tags: ['act'],
      parameters: {
        type: 'object',
        properties: {
          duration_s: { type: 'number', minimum: 1, maximum: 30, description: '按住多久（秒），默认 10。' },
          direction: { type: 'string', enum: ['left', 'right'], description: '可选：矿车需要一直按方向时填。' },
        },
      },
    },
    {
      name: 'terraria_stop',
      description: '立刻松手停下，取消正在执行的动作。主人说停、或者情况不对时马上调用。',
      tags: ['act'],
      parameters: { type: 'object', properties: {} },
    },
    {
      name: 'terraria_lookup_item',
      description: '在本地 6194 个物品表里按名字查 ID。不确定物品叫什么时先查，不要瞎猜。',
      tags: ['read'],
      parameters: {
        type: 'object',
        properties: { query: { type: 'string', description: '物品名或编号。' } },
        required: ['query'],
      },
    },
    {
      name: 'terraria_launch_game',
      description: '启动 Terraria.exe（已经在运行就切到前台），然后等主人进入世界。',
      tags: ['act'],
      parameters: { type: 'object', properties: {} },
    },
  ];
}

const clamp = (value: unknown, min: number, max: number, fallback: number): number => {
  const n = Math.floor(Number(value));
  if (!Number.isFinite(n)) return fallback;
  return Math.min(max, Math.max(min, n));
};

const num = (value: unknown, fallback: number): number => {
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
};

const one = (value: unknown, fallback = 0): number => Math.round(num(value, fallback) * 10) / 10;

/** 把 /api/state 压成一段给模型看的文字。 */
export function formatStatus(state: Record<string, unknown>, opts: { showMap: boolean; maxChars: number }): string {
  const lines: string[] = [];
  const ready = state.ready === true;
  const player = (state.player ?? {}) as PlayerLike;
  const tool = (state.tool ?? {}) as { name?: string; slot?: number };
  const inventory = Array.isArray(state.inventory) ? (state.inventory as Array<Record<string, unknown>>) : [];
  const enemies = Array.isArray(state.enemies) ? (state.enemies as EnemyLike[]) : [];
  const grid = Array.isArray(state.grid) ? (state.grid as string[]) : [];
  const radius = clamp(state.grid_radius, 0, 64, Math.floor(grid.length / 2));

  lines.push(`泰拉瑞亚｜游戏${ready ? '已就绪' : '未就绪'}｜动作：${String(state.action ?? 'Idle')}`);
  if (!ready) {
    const reason = String(state.error ?? state.game_error ?? '').trim();
    if (reason) lines.push(`未就绪原因：${reason}`);
    lines.push('下一步：terraria_launch_game 启动游戏；等进入世界后再 terraria_see。');
    return lines.join('\n');
  }

  const hp = Math.round(num(player.hp, 0));
  const maxHp = Math.round(num(player.max_hp, 100));
  lines.push(
    `你：格(${Math.floor(num(player.tile_x, 0))},${Math.floor(num(player.tile_y, 0))})` +
      `｜世界像素(${one(player.x_tiles, 0) * 16},${one(player.y_tiles, 0) * 16})` +
      `｜速度(${one(player.vx, 0)},${one(player.vy, 0)})` +
      `｜${player.on_ground ? '在地面' : '在空中'}`,
  );
  lines.push(`血量：${hp}/${maxHp}｜手上：${String(tool.name ?? '空手')}（slot ${String(tool.slot ?? '?')}）`);
  lines.push(`背包：${state.inventory_open === true ? '打开着（放方块/走路前先 close_inventory）' : '关着'}`);

  const items = inventory
    .filter((item) => num(item.stack, 0) > 0 && String(item.name ?? '').trim() !== '')
    .map((item) => `${String(item.name)}×${Math.round(num(item.stack, 1))}`);
  lines.push(`背包(${items.length})：${items.length ? items.join('、') : '空'}`);

  // 找怪范围：150×150 格（矿车/猪鲨战需要看得很远）
  const ENEMY_RANGE_TILES = 150;
  if (enemies.length) {
    const px = num(player.x_tiles, 0) * 16;
    const py = num(player.y_tiles, 0) * 16;
    const inRange = enemies
      .map((enemy) => {
        const ex = num(enemy.x, 0) + num(enemy.w, 16) / 2;
        const ey = num(enemy.y, 0) + num(enemy.h, 16) / 2;
        return { enemy, ex, ey, distance: Math.hypot(ex - px, ey - py) / 16 };
      })
      .filter(
        (entry) =>
          Math.abs(entry.ex - px) / 16 <= ENEMY_RANGE_TILES &&
          Math.abs(entry.ey - py) / 16 <= ENEMY_RANGE_TILES,
      )
      .sort((a, b) => a.distance - b.distance)
      .slice(0, 8);
    if (inRange.length) {
      const near = inRange.map(
        ({ enemy, ex, ey, distance }) =>
          `#${String(enemy.i ?? '?')} 格(${Math.floor(ex / 16)},${Math.floor(ey / 16)}) ` +
          `距离${distance.toFixed(1)}格 血${Math.round(num(enemy.life, 0))}`,
      );
      lines.push(`敌人(${inRange.length}/${enemies.length}，${ENEMY_RANGE_TILES}格内)：${near.join('；')}`);
    } else {
      lines.push(`敌人：${ENEMY_RANGE_TILES} 格内没有（更远处还有 ${enemies.length} 只）`);
    }
  } else {
    lines.push('敌人：附近没有');
  }

  if (opts.showMap && grid.length) {
    const originX = Math.floor(num(player.tile_x, 0)) - radius;
    const originY = Math.floor(num(player.tile_y, 0)) - radius;
    // 直接在“你站的这一列”往下找地面，告诉模型“能放的格子”是哪一个，
    // 避免它自己拿像素坐标换算 y 时差一两格（这是放置报 no_support 的常见原因）。
    for (let grow = radius + 1; grow < grid.length; grow += 1) {
      const ch = String((grid[grow] ?? '')[radius] ?? '.');
      if ('DSWM?'.includes(ch)) {
        lines.push(
          `脚下地面：格(${originX + radius},${originY + grow}) 是 ${ch}；` +
            `往地上放东西就把目标定在格(${originX + radius},${originY + grow - 1})`,
        );
        break;
      }
    }
    // 把怪物也叠到地图上：E。位置用世界像素换算成格，再减去地图左上角。
    const enemyCells = new Set<string>();
    for (const enemy of enemies) {
      const ex = Math.floor((num(enemy.x, 0) + num(enemy.w, 16) / 2) / 16);
      const ey = Math.floor((num(enemy.y, 0) + num(enemy.h, 16) / 2) / 16);
      enemyCells.add(`${ex},${ey}`);
    }
    const rows = grid.map((row, index) => {
      const chars = row.split('');
      for (let col = 0; col < chars.length; col += 1) {
        if (enemyCells.has(`${originX + col},${originY + index}`)) chars[col] = 'E';
      }
      if (index === radius && radius < chars.length) chars[radius] = '★';
      return chars.join('');
    });
    lines.push(
      `地图 ${grid.length}×${(grid[0] ?? '').length}（左上角=格(${originX},${originY})；★=你，T=树，D=泥土/草地，S=石，W=木块，B=墙，P=植物，E=怪物，K=工作台，?=不认识的方块，.=空气）`,
    );
    let workbench = '';
    for (let r = 0; r < grid.length && !workbench; r += 1) {
      const row = grid[r] ?? '';
      for (let c = 0; c < row.length; c += 1) {
        if (row[c] === 'K') {
          workbench = `格(${originX + c},${originY + r})`;
          break;
        }
      }
    }
    lines.push(
      workbench
        ? `附近工作台：有，在 ${workbench}`
        : '附近工作台：没有（做需要工作台的配方前，先把背包里的工作台放出来）',
    );
    lines.push(rows.join('\n'));
  }

  const text = lines.join('\n');
  if (text.length <= opts.maxChars) return text;
  return `${text.slice(0, opts.maxChars)}\n…（已截断，需要细节就看地图或再调一次）`;
}

/** 快捷栏/背包结果的统一文字。 */
export function formatSlots(label: string, data: Record<string, unknown>): string {
  const slots = Array.isArray(data.slots) ? (data.slots as SlotLike[]) : [];
  const empty = Array.isArray(data.empty) ? (data.empty as number[]) : [];
  if (!slots.length) return `${label}：全空（空位 ${empty.length} 个）`;
  const rows = slots
    .map((item) => `${String(item.slot ?? '?')}:${String(item.name ?? '?')}×${num(item.stack, 1)}`)
    .join('｜');
  return `${label}：${rows}\n空位：${empty.length ? empty.join(',') : '无'}`;
}

/** 把一次动作的 HTTP 结果翻译成桌宠看得懂的回执。 */
export function describeOutcome(label: string, outcome: HttpOutcome<AgentActionResult>): ToolOutcome {
  if (outcome.status === 0) {
    return {
      text: `${label}：连不上本地 Agent 服务。${outcome.error}`,
      failed: true,
    };
  }
  const result = outcome.data?.result;
  if (!result) {
    return {
      text: `${label}：服务返回了无法解析的内容（HTTP ${outcome.status}）。${outcome.error}`,
      failed: true,
    };
  }
  const status = String(result.status ?? '');
  const message = String(result.message ?? '').trim() || status;
  if (result.ok) return { text: `${label}完成：${message}` };

  const data = (result.data ?? {}) as Record<string, unknown>;
  let hint = '';

  if (status === 'too_far') {
    const dx = num(data.dx, 0);
    const dy = num(data.dy, 0);
    if (Math.abs(dx) >= 1) {
      const direction = dx > 0 ? 'right' : 'left';
      const steps = Math.max(0.5, Math.min(20, Math.abs(dx) - 2));
      hint = `目标在${dx > 0 ? '右' : '左'}边 ${Math.abs(dx)} 格、上下 ${Math.abs(dy)} 格：先调 terraria_move {direction:"${direction}", distance:${steps}} 走近，再试一次。`;
    } else {
      hint = `目标在正上方/下方 ${Math.abs(dy)} 格：先找台阶或跳过去，再试一次。`;
    }
  } else {
    const hints: Record<string, string> = {
      game_not_ready: '游戏没开或还没进世界：先调 terraria_launch_game。',
      busy: '上一个动作还没做完：等它结束，或者先 terraria_stop。',
      inventory_closed: '背包没打开：先 terraria_open_inventory，再试一次。',
      inventory_grid_closed: '看不到背包格子（可能只开着箱子/商店）：先 terraria_open_inventory；它会按一次 Esc 并确认格子出现。',
      inventory_not_visible: '背包格子没出来：确认游戏在前台，或者手动按一次 Esc；旁边有箱子/商店时先走开一步。',
      inventory_open: '背包还开着：先 terraria_close_inventory，再试一次。',
      still_open: '背包/箱子/商店还开着：先 terraria_move 走开一步（箱子会自动关），再 close_inventory。',
      toggle_ineffective: '背包键没生效：确认游戏在前台；如果改过背包键，到插件配置里把「背包键」改成游戏里的那个键。',
      no_tree_in_view: '视野里没有树：先 terraria_move 往有树的方向走。',
      too_far_x: '',
      no_block: '附近没有可挖的方块：先 terraria_move 换个地方。',
      item_not_found: '背包里没有这个东西：先 terraria_check_inventory / terraria_check_hotbar 看清楚名字。',
      slot_empty: '那个格子是空的：先用 check 看一眼再搬。',
      move_not_verified: '搬完没确认到：可能格子被换掉了，用 terraria_check_hotbar 再看一眼。',
      place_failed: '放置失败：先看回执里的原因；如果是“鼠标没移动”，把游戏点到前台，或者用管理员权限启动 Agent 再试。',
      not_placed: '方块没放上：手上拿的可能是工具而不是方块；先 move_item 放进快捷栏，再 select_slot。',
      bad_slot: '格号不对：快捷栏 1-10，背包 1-50。',
      bad_target: '坐标不对：需要整数 x 和 y。',
      recipe_not_found: '制作菜单里找不到这个配方：先 check_inventory 看材料够不够。',
      craft_no_change: '点了配方但背包没变化：材料可能不够。',
      craft_not_increased: '点了配方但数量没增加：材料可能不够。',
      no_target: '没告诉我要做什么：给一个物品名。',
      not_in_hotbar: '快捷栏里没有它：先用 terraria_check_inventory 找，再 terraria_move_item 搬进快捷栏。',
      no_query: '要告诉我在快捷栏里找什么物品名。',
      already_solid: '这一格已经有方块了：换一个相邻的空气格再放。',
      in_player_body: '那格是你自己站着的身体：放到身体左边或右边一格，不要放在自己身上。',
      no_space: '身体左右两边都放不下这么高的柱子：先走到空旷一点的地方。',
      jump_to_pillar_failed: '跳不上柱顶：把柱子降到 4 格再试，或先放一级台阶。',
      no_ground: '脚下没有实心地面：先站到平地上再搭。',
      no_ground: '脚下没有实心地面：先站到地面上再搭。',
      block_occupied: '那格被方块占着：墙只能刷在空气格。',
      not_walled: '墙没刷上：确认手上是「木墙」、距离不超过 5 格。',
      not_holding_block: '手上不是方块：先用 find_in_hotbar 找到木材/土块，再 select_slot 选中，再放。',
      not_holding_wall: '手上不是背景墙：先 select_slot 选中「木墙」再刷。',
      needs_anchor: '墙不能悬空放：从地面或已有墙旁边开始，一格一格往外扩。',
      partial: '刷了一部分：看回执里 blocked 的格子，先把它们旁边的墙/方块补上。',
      wall_place_failed: '刷墙失败：先看回执里的原因。',
      area_too_big: '区域太大：一次最多 60 格，分几次刷。',
      not_valid_house: '房子还不合格：按回执里“缺”的项补。',
      no_space: '附近没有可通行的室内空间：先搭墙围起来。',
      no_support: '目标周围没有支撑块：放地上要先站在地面上；放左右要先有一块邻块。',
      no_material: '背包里没有可用的方块：先去砍树或挖土。',
      out_of_range: '目标太远：先 terraria_move 走近再放。',
      unknown_action: '这个动作本地 Agent 不认识：插件和 Agent 版本不匹配。',
      switch_not_applied: '数字键没生效：先确认游戏在前台，或者点一下游戏窗口再试。',
    };
    hint = hints[status] ?? '';
  }

  return {
    text: `${label}失败（${status}）：${message}${hint ? `\n提示：${hint}` : ''}`,
    failed: true,
  };
}

export interface TerrariaWorldOptions {
  cfg?: Partial<TerrariaConfig>;
  dataDir?: string;
  persist?: (patch: Partial<TerrariaConfig>) => void;
}

export class TerrariaWorld {
  readonly id = WORLD_ID;
  private readonly cfgRef: Partial<TerrariaConfig>;
  private readonly dataDir: string;
  private readonly persistFn?: (patch: Partial<TerrariaConfig>) => void;
  private readonly client: TerrariaAgent;

  constructor(opts: TerrariaWorldOptions = {}) {
    this.cfgRef = opts.cfg ?? {};
    this.dataDir = opts.dataDir ?? '';
    this.persistFn = opts.persist;
    this.client = new TerrariaAgent({
      baseUrl: () => this.cfg().panelUrl,
      timeoutMs: () => this.cfg().actionTimeoutMs,
      autoStart: () => this.cfg().autoStart,
      pythonExe: () => this.cfg().pythonExe,
      projectDir: () => this.cfg().projectDir,
    });
  }

  /** 每次读取都合并最新配置：桌宠设置里改了立刻生效，不用重启 World。 */
  cfg(): TerrariaConfig {
    return { ...DEFAULTS, ...this.cfgRef };
  }

  /** 提示词里的环境状态；前缀每次重建都会调用，必须便宜。 */
  async envPromptVars(): Promise<Record<string, string>> {
    const cfg = this.cfg();
    const panel = cfg.panelUrl.trim() || DEFAULTS.panelUrl;
    const lines: string[] = [];
    if (!(await this.client.pingCached(5000))) {
      lines.push(`本地服务：未连接（${panel}）${cfg.autoStart ? '；第一次调用工具会自动帮你拉起' : ''}`);
    } else {
      lines.push(`本地服务：在线（${panel}）`);
      const state = await this.client.state(true);
      if (state.ok && state.data) {
        const ready = state.data.ready === true;
        const reason = String(state.data.error ?? '').trim();
        lines.push(`游戏：${ready ? '已就绪' : `未就绪${reason ? `（${reason}）` : ''}`}`);
        if (ready) {
          const player = (state.data.player ?? {}) as PlayerLike;
          lines.push(
            `你：格(${Math.floor(num(player.tile_x, 0))},${Math.floor(num(player.tile_y, 0))})` +
              `｜血量 ${Math.round(num(player.hp, 0))}/${Math.round(num(player.max_hp, 100))}`,
          );
        }
      } else {
        lines.push(`游戏：状态读取失败（${state.error}）`);
      }
    }
    lines.push(`面板：${panel}`);
    return { 'terraria.status': lines.join('\n'), 'terraria.panel': panel };
  }

  tools() {
    const handlers: Record<string, (args: Record<string, unknown>) => Promise<ToolOutcome | string>> = {
      terraria_see: () => this.see(),
      terraria_move: (args) => this.move(args),
      terraria_jump: (args) => this.jump(args),
      terraria_jump_onto: (args) => this.jumpOnto(args),
      terraria_chop_tree: () => this.doAction('chop_tree', { local_only: true }, '砍树'),
      terraria_mine: (args) => {
        const x = Number(args.x);
        const y = Number(args.y);
        if (Number.isFinite(x) && Number.isFinite(y)) {
          return this.doAction('mine_block', { local_only: true, x: Math.floor(x), y: Math.floor(y) }, `挖 (${Math.floor(x)},${Math.floor(y)})`);
        }
        return this.doAction('mine_block', { local_only: true }, '挖方块');
      },
      terraria_place: (args) => this.place(args),
      terraria_build_pillar: (args) => this.buildPillar(args),
      terraria_build_frame: (args) => this.buildFrame(args),
      terraria_build_blueprint: (args) => this.buildBlueprint(args),
      terraria_place_wall: (args) => this.placeWall(args),
      terraria_place_wall_area: (args) => this.placeWallArea(args),
      terraria_check_house: () => this.doAction('check_house', {}, '检查房子'),
      terraria_open_inventory: () => this.doAction('open_inventory', { key: this.cfg().inventoryKey || 'escape' }, '打开背包'),
      terraria_close_inventory: () => this.doAction('close_inventory', { key: this.cfg().inventoryKey || 'escape' }, '关闭背包'),
      terraria_check_hotbar: () => this.checkSlots('check_hotbar', '快捷栏'),
      terraria_check_inventory: () => this.checkSlots('check_inventory', '主背包'),
      terraria_find_in_hotbar: (args) => this.findHotbar(args),
      terraria_move_item: (args) => this.moveItem(args),
      terraria_select_slot: (args) => this.selectSlot(args),
      terraria_craft: (args) => this.craft(args),
      terraria_fight: (args) => this.fight(args),
      terraria_hold_fire: (args) => this.holdFire(args),
      terraria_stop: () => this.doAction('stop', {}, '停止'),
      terraria_lookup_item: (args) => this.lookupItem(args),
      terraria_launch_game: () => this.launchGame(),
    };
    return toolSpecs().map((spec) => ({
      ...spec,
      handler: (args: Record<string, unknown>) => handlers[spec.name](args ?? {}),
    }));
  }

  webUi(language?: string) {
    const zh = !language || language.toLowerCase().startsWith('zh');
    const cfg = this.cfg();
    const panel = cfg.panelUrl.trim() || DEFAULTS.panelUrl;
    const label = zh ? '泰拉瑞亚' : 'Terraria';
    return {
      label,
      panels: [
        {
          id: 'control',
          title: zh ? '控制面板' : 'Control panel',
          description: zh
            ? '在桌宠里直接看地图、血量、背包和日志，也能下指令。数据走本地服务，不需要嵌网页。'
            : 'Map, HP, inventory and logs right inside the pet, plus a command box.',
        },
      ],
      invoke: async (panelId: string, method: string, args: unknown[]) => {
        if (panelId !== 'control') throw new Error(`unknown panel: ${panelId}`);
        if (method === 'state') return this.panelState();
        if (method === 'start') {
          const problem = await this.client.ensureService();
          if (problem) throw new Error(problem);
          return this.panelState();
        }
        if (method === 'command') {
          const text = String(args?.[0] ?? '').trim();
          if (!text) throw new Error('指令不能为空');
          const problem = await this.client.ensureService();
          if (problem) throw new Error(problem);
          const out = await this.client.command(text);
          if (!out.ok) throw new Error(out.error || '指令发送失败');
          return out.data ?? { ok: true };
        }
        if (method === 'action') {
          const action = String(args?.[0] ?? '').trim();
          const params = (args?.[1] ?? {}) as Record<string, unknown>;
          if (!action) throw new Error('缺少动作名');
          const problem = await this.client.ensureService();
          if (problem) throw new Error(problem);
          const out = await this.client.action(action, params);
          return out.data ?? { ok: false };
        }
        if (method === 'blueprint.get') {
          return { rows: blueprintRows };
        }
        if (method === 'blueprint.save') {
          const raw = args?.[0];
          if (!Array.isArray(raw) || raw.length === 0) throw new Error('蓝图是空的');
          const cleaned = raw
            .map((row) => String(row ?? '').replace(/[^01]/g, ''))
            .filter((row) => row.length > 0);
          if (!cleaned.length) throw new Error('蓝图里没有格子');
          blueprintRows = cleaned;
          try {
            writeFileSync(BLUEPRINT_PATH, JSON.stringify({ rows: cleaned, savedAt: new Date().toISOString() }, null, 2));
          } catch {
            // 写文件失败只影响持久化，内存里仍然可用
          }
          return { rows: cleaned };
        }
        if (method === 'stop') {
          const out = await this.client.command('停止');
          return out.data ?? { ok: true };
        }
        throw new Error(`unknown method: ${method}`);
      },
      config: [
        {
          id: 'world:terraria',
          owner: 'world:terraria' as const,
          schema: {
            type: 'object' as const,
            title: label,
            description: zh
              ? '桌宠通过本机 Terraria-Agent 服务操控官方泰拉瑞亚。控制面板地址就写在这里。'
              : 'The pet plays official Terraria through the local Terraria-Agent service. The control panel URL lives here.',
            properties: {
              'worlds.terraria.enabled': {
                type: 'boolean' as const,
                title: zh ? '启用泰拉瑞亚' : 'Enable Terraria',
                description: zh ? '打开后桌宠才能调用泰拉瑞亚工具。' : 'Turn on so the pet can use the Terraria tools.',
                'x-hot': true,
              },
              'worlds.terraria.panelUrl': {
                type: 'string' as const,
                title: zh ? '控制面板地址' : 'Control panel URL',
                description: zh
                  ? '本机 Agent 的网页面板地址，插件也用它连接服务。默认 http://127.0.0.1:5000。'
                  : 'URL of the local agent web panel; the plugin also connects here. Default http://127.0.0.1:5000.',
                'x-hot': true,
              },
              'worlds.terraria.pythonExe': {
                type: 'string' as const,
                title: zh ? 'Python 解释器' : 'Python executable',
                description: zh ? '运行 main.py 的 python.exe（项目的 .venv 里那个）。' : 'python.exe used to run main.py (the project venv one).',
                'x-path': { kind: 'file' as const, extensions: ['exe'] },
                'x-hot': true,
              },
              'worlds.terraria.projectDir': {
                type: 'string' as const,
                title: zh ? '项目目录' : 'Project directory',
                description: zh ? '包含 main.py 的 Terraria-Agent 目录。' : 'Terraria-Agent directory containing main.py.',
                'x-path': { kind: 'directory' as const },
                'x-hot': true,
              },
              'worlds.terraria.autoStart': {
                type: 'boolean' as const,
                title: zh ? '自动拉起服务' : 'Auto-start service',
                description: zh ? '服务没运行时，第一次调用工具自动启动 main.py。' : 'Start main.py on first tool call when the service is down.',
                'x-hot': true,
              },
              'worlds.terraria.inventoryKey': {
                type: 'string' as const,
                title: zh ? '背包键' : 'Inventory key',
                description: zh
                  ? '游戏里打开物品栏的按键名（pydirectinput 键名）。默认 escape；如果你在游戏里改过键，就填那个键，例如 i。'
                  : 'Key name for the inventory (pydirectinput name). Default escape; change it if you rebound the key in game, e.g. i.',
                'x-hot': true,
              },
              'worlds.terraria.actionTimeoutMs': {
                type: 'integer' as const,
                title: zh ? '单个动作超时（毫秒）' : 'Action timeout (ms)',
                description: zh ? '长动作要留足时间。' : 'Long actions need room.',
                minimum: 5000,
                maximum: 600000,
                'x-hot': true,
              },
              'worlds.terraria.showMap': {
                type: 'boolean' as const,
                title: zh ? '状态里带地形图' : 'Include tile map in status',
                description: zh ? '关掉能省 token，但桌宠就看不到附近地形了。' : 'Turning it off saves tokens but hides nearby terrain.',
                'x-hot': true,
              },
              'worlds.terraria.maxStatusChars': {
                type: 'integer' as const,
                title: zh ? '状态回执上限（字符）' : 'Status reply limit (chars)',
                description: zh ? '超出会截断，防止一次塞太多 token。' : 'Truncated beyond this to cap tokens.',
                minimum: 500,
                maximum: 20000,
                'x-hot': true,
              },
            },
          },
        },
      ],
      links: [{ label: zh ? '打开网页版面板' : 'Open web panel', href: panel, inheritTheme: true }],
      promptDocs: [
        {
          key: 'worlds.terraria.envPrompt',
          title: zh ? '泰拉瑞亚环境提示' : 'Terraria environment prompt',
          description: zh ? '告诉桌宠怎么用这些小动作拼出砍树、做工作台、放家具。' : 'How the pet composes the small actions.',
          path: join(PKG_DIR, 'ENV_PROMPT.md'),
          role: 'envPrompt' as const,
          vars: [
            { name: 'terraria.status', description: zh ? '服务与游戏状态。' : 'Service and game status.' },
            { name: 'terraria.panel', description: zh ? '控制面板地址。' : 'Control panel URL.' },
          ],
        },
      ],
    };
  }

  async start(): Promise<void> {
    // 不主动连服务：避免桌宠一启动就去 spawn 游戏相关进程。
  }

  async stop(): Promise<void> {
    // 无长连接需要关闭。
  }

  // ------------------------------------------------------------------
  // 面板数据
  // ------------------------------------------------------------------

  private async panelState(): Promise<Record<string, unknown>> {
    const cfg = this.cfg();
    const panel = cfg.panelUrl.trim() || DEFAULTS.panelUrl;
    if (!(await this.client.pingCached(1500))) {
      return { online: false, panelUrl: panel, autoStart: cfg.autoStart };
    }
    const state = await this.client.state(false);
    if (!state.ok || !state.data) {
      return { online: false, panelUrl: panel, autoStart: cfg.autoStart, error: state.error };
    }
    const data = state.data;
    const logs = Array.isArray(data.logs) ? (data.logs as Array<Record<string, unknown>>).slice(-12) : [];
    return {
      online: true,
      panelUrl: panel,
      ready: data.ready === true,
      error: String(data.error ?? ''),
      busy: data.busy === true,
      action: String(data.action ?? 'Idle'),
      inventoryOpen: data.inventory_open === true,
      player: data.player ?? {},
      tool: data.tool ?? {},
      inventory: data.inventory ?? [],
      enemies: data.enemies ?? [],
      grid: data.grid ?? [],
      gridRadius: num(data.grid_radius, 15),
      logs,
    };
  }

  // ------------------------------------------------------------------
  // 工具实现
  // ------------------------------------------------------------------

  private async see(): Promise<ToolOutcome | string> {
    const problem = await this.client.ensureService();
    if (problem) return { text: problem, failed: true };
    const state = await this.client.state(true);
    if (!state.ok || !state.data) {
      return {
        text: `看状态失败：连不上本地服务（${this.client.baseUrl()}）。${state.error}`,
        failed: true,
      };
    }
    const cfg = this.cfg();
    return formatStatus(state.data, { showMap: cfg.showMap, maxChars: cfg.maxStatusChars });
  }

  private async move(args: Record<string, unknown>): Promise<ToolOutcome> {
    const direction = String(args.direction ?? '').trim();
    if (direction !== 'left' && direction !== 'right') {
      return { text: 'terraria_move 需要 direction=left 或 right。', failed: true };
    }
    const distance = Math.min(20, Math.max(0.5, num(args.distance, 3)));
    return this.doAction('move', { direction, distance }, `向${direction === 'left' ? '左' : '右'}走 ${distance} 格`);
  }

  private async jump(args: Record<string, unknown>): Promise<ToolOutcome> {
    const height = Math.min(6, Math.max(0, num(args.height, 1)));
    const params: Record<string, unknown> = { height };
    const direction = String(args.direction ?? '').trim();
    if (direction === 'left' || direction === 'right') params.direction = direction;
    return this.doAction('jump', params, `跳 ${height} 格`);
  }

  private async jumpOnto(args: Record<string, unknown>): Promise<ToolOutcome> {
    const x = Math.floor(num(args.x, Number.NaN));
    const y = Math.floor(num(args.y, Number.NaN));
    if (!Number.isFinite(x) || !Number.isFinite(y)) {
      return { text: 'terraria_jump_onto 需要整数 x 和 y（方块格坐标）。', failed: true };
    }
    return this.doAction('jump_onto', { x, y }, `跳上 (${x},${y})`);
  }

  private async placeWall(args: Record<string, unknown>): Promise<ToolOutcome> {
    const x = Math.floor(num(args.x, Number.NaN));
    const y = Math.floor(num(args.y, Number.NaN));
    if (!Number.isFinite(x) || !Number.isFinite(y)) {
      return { text: 'terraria_place_wall 需要整数 x 和 y。', failed: true };
    }
    return this.doAction('place_wall', { x, y }, `刷墙 (${x},${y})`);
  }

  private async placeWallArea(args: Record<string, unknown>): Promise<ToolOutcome> {
    const x1 = Math.floor(num(args.x1, Number.NaN));
    const y1 = Math.floor(num(args.y1, Number.NaN));
    const x2 = Math.floor(num(args.x2, Number.NaN));
    const y2 = Math.floor(num(args.y2, Number.NaN));
    if (![x1, y1, x2, y2].every((v) => Number.isFinite(v))) {
      return { text: 'terraria_place_wall_area 需要整数 x1,y1,x2,y2。', failed: true };
    }
    return this.doAction('place_wall_area', { x1, y1, x2, y2 }, '批量刷墙');
  }

  private async buildBlueprint(args: Record<string, unknown>): Promise<ToolOutcome> {
    if (!blueprintRows.length) {
      return {
        text: '还没有保存蓝图：让主人在控制面板的“蓝图”里点格子画好，再点“保存蓝图”。',
        failed: true,
      };
    }
    const direction = String(args.direction ?? 'right').trim() === 'left' ? 'left' : 'right';
    const width = blueprintRows[0]?.length ?? 0;
    return this.doAction(
      'build_blueprint',
      { rows: blueprintRows, direction },
      `按蓝图施工（${width}×${blueprintRows.length}）`,
    );
  }

  private async buildFrame(args: Record<string, unknown>): Promise<ToolOutcome> {
    const params: Record<string, unknown> = {
      width: Math.min(20, Math.max(3, Math.floor(num(args.width, 11)))),
      height: Math.min(10, Math.max(2, Math.floor(num(args.height, 5)))),
      pillar: Math.min(6, Math.max(2, Math.floor(num(args.pillar, 6)))),
    };
    const direction = String(args.direction ?? '').trim();
    if (direction === 'left' || direction === 'right') params.direction = direction;
    return this.doAction('build_frame', params, '按图纸搭方框');
  }

  private async buildPillar(args: Record<string, unknown>): Promise<ToolOutcome> {
    const height = Math.min(8, Math.max(1, Math.floor(num(args.height, 6))));
    const side = String(args.side ?? '').trim();
    const params: Record<string, unknown> = { height };
    if (side === 'left' || side === 'right') params.side = side;
    return this.doAction('build_pillar', params, `搭 ${height} 格柱子`);
  }

  private async place(args: Record<string, unknown>): Promise<ToolOutcome> {
    const x = Math.floor(num(args.x, Number.NaN));
    const y = Math.floor(num(args.y, Number.NaN));
    if (!Number.isFinite(x) || !Number.isFinite(y)) {
      return { text: 'terraria_place 需要整数 x 和 y（世界格坐标）。', failed: true };
    }
    return this.doAction('place_block', { x, y }, `放东西到 (${x},${y})`);
  }

  private async moveItem(args: Record<string, unknown>): Promise<ToolOutcome> {
    const toSlot = Math.floor(num(args.to_slot, Number.NaN));
    if (!Number.isFinite(toSlot) || toSlot < 1 || toSlot > 50) {
      return { text: 'terraria_move_item 需要 to_slot=1..50。', failed: true };
    }
    const params: Record<string, unknown> = { to_slot: toSlot };
    const item = String(args.item ?? '').trim();
    if (item) params.item = item;
    const fromSlot = Math.floor(num(args.from_slot, Number.NaN));
    if (!item && Number.isFinite(fromSlot)) params.from_slot = fromSlot;
    const label = item ? `把「${item}」搬到 slot ${toSlot}` : `把 slot ${fromSlot} 搬到 slot ${toSlot}`;
    const outcome = await this.action('move_item', params);
    const result = outcome.data?.result;
    if (result?.ok) {
      return `搬运完成：${String(result.message ?? '')}`;
    }
    return describeOutcome(label, outcome);
  }

  private async selectSlot(args: Record<string, unknown>): Promise<ToolOutcome> {
    const slot = Math.floor(num(args.slot, Number.NaN));
    if (!Number.isFinite(slot) || slot < 1 || slot > 10) {
      return { text: 'terraria_select_slot 需要 slot=1..10。', failed: true };
    }
    const outcome = await this.action('select_slot', { slot });
    const result = outcome.data?.result;
    if (result?.ok) return `切换完成：${String(result.message ?? '')}`;
    return describeOutcome(`切到 slot ${slot}`, outcome);
  }

  private async checkSlots(action: string, label: string): Promise<ToolOutcome | string> {
    const outcome = await this.action(action, {});
    const result = outcome.data?.result;
    if (!result) return describeOutcome(label, outcome);
    if (!result.ok) return describeOutcome(label, outcome);
    const data = (result.data ?? {}) as Record<string, unknown>;
    const text = formatSlots(label, data);
    if (action === 'check_hotbar' && data.selected !== undefined) {
      return `${text}\n当前选中：slot ${String(data.selected)}`;
    }
    return text;
  }

  private async findHotbar(args: Record<string, unknown>): Promise<ToolOutcome> {
    const item = String(args.item ?? '').trim();
    if (!item) return { text: 'terraria_find_in_hotbar 需要 item（物品名）。', failed: true };
    const outcome = await this.action('find_hotbar', { item });
    const result = outcome.data?.result;
    if (result?.ok) {
      const data = (result.data ?? {}) as Record<string, unknown>;
      const text = String(result.message ?? `「${item}」找到了`);
      const slot = Number(data.slot);
      if (Number.isFinite(slot)) {
        return `${text}\n下一步：terraria_select_slot {slot:${slot}}`;
      }
      return text;
    }
    return describeOutcome(`找「${item}」`, outcome);
  }

  private async craft(args: Record<string, unknown>): Promise<ToolOutcome> {
    const item = String(args.item ?? '').trim();
    if (!item) return { text: 'terraria_craft 需要 item（物品中文名）。', failed: true };
    const params: Record<string, unknown> = { item, require_open: true };
    const count = Math.floor(num(args.count, 1));
    if (count > 1) params.count = Math.min(500, count);
    return this.doAction('craft', params, count > 1 ? `合成 ${item} ×${count}` : `合成 ${item}`);
  }

  private async holdFire(args: Record<string, unknown>): Promise<ToolOutcome> {
    const duration = Math.min(30, Math.max(1, num(args.duration_s, 10)));
    const params: Record<string, unknown> = { duration_s: duration };
    const direction = String(args.direction ?? '').trim();
    if (direction === 'left' || direction === 'right') params.direction = direction;
    return this.doAction('hold_fire', params, `持续开火 ${duration} 秒`);
  }

  private async fight(args: Record<string, unknown>): Promise<ToolOutcome> {
    const duration = Math.min(12, Math.max(0.5, num(args.duration_s, 4)));
    return this.doAction('fight', { duration_s: duration }, `战斗 ${duration} 秒`);
  }

  private async lookupItem(args: Record<string, unknown>): Promise<ToolOutcome> {
    const query = String(args.query ?? '').trim();
    if (!query) return { text: 'terraria_lookup_item 需要 query。', failed: true };
    const problem = await this.client.ensureService();
    if (problem) return { text: problem, failed: true };
    const outcome = await this.client.items(query);
    if (!outcome.ok || !outcome.data) {
      return { text: `查询失败：${outcome.error}`, failed: true };
    }
    const items = outcome.data.items ?? [];
    if (!items.length) return `没有找到「${query}」；换个关键字试试。`;
    return items.map((item) => `${String(item.name ?? '?')}#${String(item.id ?? '?')}`).join('、');
  }

  private async launchGame(): Promise<ToolOutcome> {
    const problem = await this.client.ensureService();
    if (problem) return { text: problem, failed: true };
    const outcome = await this.client.launchGame();
    if (!outcome.ok) {
      const message = outcome.data?.message || outcome.error;
      return { text: `启动游戏失败：${message}`, failed: true };
    }
    const message = String(outcome.data?.message ?? '游戏已启动');
    return `${message}\n下一步：等主人进入世界后调 terraria_see。`;
  }

  private async action(action: string, params: Record<string, unknown>): Promise<HttpOutcome<AgentActionResult>> {
    const problem = await this.client.ensureService();
    if (problem) {
      return {
        ok: false,
        status: 0,
        data: null,
        error: problem,
      };
    }
    return this.client.action(action, params);
  }

  /** 统一的动作出口：先保证服务在，再把结果翻译成话。 */
  private async doAction(
    action: string,
    params: Record<string, unknown>,
    label: string,
  ): Promise<ToolOutcome> {
    const outcome = await this.action(action, params);
    return describeOutcome(label, outcome);
  }
}

export function defineWorld() {
  return {
    id: WORLD_ID,
    get label() {
      return '泰拉瑞亚';
    },
    defaults: (): TerrariaConfig => ({ ...DEFAULTS }),
    create: (ctx: {
      cfg?: Partial<TerrariaConfig>;
      dataDir?: string;
      persist?: (patch: Partial<TerrariaConfig>) => void;
    }) =>
      new TerrariaWorld({
        cfg: ctx?.cfg ?? {},
        dataDir: ctx?.dataDir ?? '',
        persist: ctx?.persist,
      }),
    describe: {
      tools: toolSpecs().map((spec) => ({
        name: spec.name,
        description: spec.description,
        parameters: spec.parameters,
        tags: spec.tags,
        summary: TOOL_SUMMARIES[spec.name],
      })),
      events: [],
    },
  };
}

export default defineWorld;
