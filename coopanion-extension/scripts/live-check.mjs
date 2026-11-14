/**
 * 真机联调：连本机正在运行的 Terraria-Agent 服务（默认 127.0.0.1:5000）。
 *
 * 不要求游戏已启动：没有游戏时会验证“未就绪”回执是否正确。
 * 运行：node scripts/live-check.mjs
 */
import definition from '../src/index.ts';

const panelUrl = process.env.TERRARIA_PANEL ?? 'http://127.0.0.1:5000';
const world = definition.create({
  cfg: { panelUrl, autoStart: false, showMap: true },
  dataDir: '',
});
const tools = Object.fromEntries(world.tools().map((tool) => [tool.name, tool]));
const asText = (value) => (typeof value === 'string' ? value : value.text);

console.log('== 探活 ==');
console.log('服务在线：', await world.cfg().panelUrl);

console.log('\n== terraria_see ==');
console.log(asText(await tools.terraria_see.handler({})));

console.log('\n== terraria_lookup_item(铁剑) ==');
console.log(asText(await tools.terraria_lookup_item.handler({ query: '铁剑' })));

console.log('\n== terraria_stop ==');
const stop = await tools.terraria_stop.handler({});
console.log(asText(stop), '\n标记失败：', Boolean(stop.failed));

console.log('\n== 环境提示词变量 ==');
const vars = await world.envPromptVars();
console.log(vars['terraria.status']);
