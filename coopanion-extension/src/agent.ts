/**
 * 与本地 Terraria-Agent 的 Python 服务通信。
 *
 * 约定（见 Terraria-Agent/app.py）：
 *   GET  /api/health        服务是否在线
 *   GET  /api/state?lite=1  玩家/背包/地图/敌人（不含日志与配置）
 *   POST /api/action        {"action": "...", "params": {...}} 直接执行技能
 *   GET  /api/items?q=...   本地物品表查询
 *   POST /api/game/launch   启动 Terraria.exe
 *
 * 服务没起来时（比如主人刚开机），插件可以自己 spawn 一次 `main.py` 并等它就绪。
 * 这里不互相 import 框架模块，方便脱离桌宠单独测试。
 */

import { spawn } from 'node:child_process';

export interface AgentOptions {
  /** 面板地址，例如 http://127.0.0.1:5000 */
  baseUrl(): string;
  /** 单个动作最长等待时间（毫秒） */
  timeoutMs(): number;
  /** 服务没起来时要不要自动拉起 */
  autoStart(): boolean;
  /** 运行 main.py 的 Python 解释器 */
  pythonExe(): string;
  /** Terraria-Agent 项目目录（含 main.py） */
  projectDir(): string;
}

export interface HttpOutcome<T> {
  ok: boolean;
  status: number;
  data: T | null;
  error: string;
}

export interface AgentActionResult {
  ok?: boolean;
  action?: string;
  result?: {
    ok?: boolean;
    status?: string;
    message?: string;
    data?: Record<string, unknown>;
  };
  busy?: boolean;
}

const sleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

const errorText = (err: unknown): string => (err instanceof Error ? err.message : String(err));

export class TerrariaAgent {
  private readonly options: AgentOptions;
  private starting: Promise<boolean> | null = null;
  private pingAt = 0;
  private pingOk = false;

  constructor(options: AgentOptions) {
    this.options = options;
  }

  baseUrl(): string {
    return this.options.baseUrl().trim().replace(/\/+$/, '');
  }

  /** 单次 HTTP 请求；任何异常都转成 { ok:false, error }，不向外抛。 */
  private async call<T>(
    path: string,
    init: { method?: string; body?: unknown; timeoutMs?: number } = {},
  ): Promise<HttpOutcome<T>> {
    const timeout = Math.max(1000, Math.floor(init.timeoutMs ?? this.options.timeoutMs()));
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    try {
      const response = await fetch(`${this.baseUrl()}${path}`, {
        method: init.method ?? 'GET',
        headers: init.body === undefined ? undefined : { 'Content-Type': 'application/json' },
        body: init.body === undefined ? undefined : JSON.stringify(init.body),
        signal: controller.signal,
      });
      const raw = await response.text();
      let data: unknown = null;
      try {
        data = raw ? JSON.parse(raw) : null;
      } catch {
        data = null;
      }
      if (!response.ok) {
        const detail =
          data && typeof data === 'object' && 'error' in data
            ? String((data as { error?: unknown }).error ?? '')
            : '';
        return {
          ok: false,
          status: response.status,
          data: data as T | null,
          error: detail || raw.slice(0, 300) || `HTTP ${response.status}`,
        };
      }
      return { ok: true, status: response.status, data: data as T, error: '' };
    } catch (err) {
      const aborted = err instanceof Error && err.name === 'AbortError';
      return {
        ok: false,
        status: 0,
        data: null,
        error: aborted ? `请求超时（${Math.round(timeout / 1000)} 秒）` : errorText(err),
      };
    } finally {
      clearTimeout(timer);
    }
  }

  /** 轻量探活，2 秒缓存；给提示词和工具入口共用，避免每步都打服务。 */
  async pingCached(ttlMs = 3000): Promise<boolean> {
    const now = Date.now();
    if (now - this.pingAt < ttlMs) return this.pingOk;
    const outcome = await this.call<{ ok?: boolean }>('/api/health', { timeoutMs: 2500 });
    this.pingAt = now;
    this.pingOk = outcome.ok && outcome.data?.ok !== false;
    return this.pingOk;
  }

  /** 保证服务在线：在线直接返回 null，否则按配置自动拉起。 */
  async ensureService(): Promise<string | null> {
    if (await this.pingCached(0)) return null;
    if (!this.options.autoStart()) {
      return `本地服务没有运行（${this.baseUrl()}）。请先启动 Terraria-Agent（main.py 或 start.bat），或在插件配置里打开“自动拉起服务”。`;
    }
    if (!this.starting) {
      this.starting = this.launchService();
    }
    try {
      const ok = await this.starting;
      if (ok) {
        this.pingAt = 0;
        return null;
      }
    } finally {
      this.starting = null;
    }
    return `自动拉起本地服务失败（${this.baseUrl()}）。请检查插件配置里的 Python 路径和项目目录，或手动运行 start.bat。`;
  }

  private async launchService(): Promise<boolean> {
    const python = this.options.pythonExe().trim();
    const projectDir = this.options.projectDir().trim();
    if (!python || !projectDir) return false;
    try {
      const child = spawn(python, ['main.py'], {
        cwd: projectDir,
        detached: true,
        stdio: 'ignore',
        windowsHide: true,
      });
      child.unref();
    } catch {
      return false;
    }
    // main.py 里的 ClrMD 探测偶尔要几秒，最多等 25 秒。
    for (let attempt = 0; attempt < 50; attempt += 1) {
      await sleep(500);
      const outcome = await this.call<{ ok?: boolean }>('/api/health', { timeoutMs: 1500 });
      if (outcome.ok) return true;
    }
    return false;
  }

  async state(lite = true): Promise<HttpOutcome<Record<string, unknown>>> {
    const path = lite ? '/api/state?lite=1' : '/api/state';
    return this.call<Record<string, unknown>>(path, { timeoutMs: 10000 });
  }

  /** 网页版面板的指令入口；走本地关键词/大模型路由，返回很快（202）。 */
  async command(text: string): Promise<HttpOutcome<{ ok?: boolean; message?: string; reply?: string }>> {
    return this.call('/api/command', {
      method: 'POST',
      body: { command: text },
      timeoutMs: 30000,
    });
  }

  async action(name: string, params: Record<string, unknown> = {}): Promise<HttpOutcome<AgentActionResult>> {
    return this.call<AgentActionResult>('/api/action', {
      method: 'POST',
      body: { action: name, params },
      timeoutMs: this.options.timeoutMs(),
    });
  }

  async items(query: string): Promise<HttpOutcome<{ ok?: boolean; items?: Array<{ id?: number; name?: string }> }>> {
    return this.call(`/api/items?q=${encodeURIComponent(query)}&limit=12`, { timeoutMs: 8000 });
  }

  async launchGame(): Promise<HttpOutcome<{ ok?: boolean; message?: string }>> {
    return this.call('/api/game/launch', { method: 'POST', timeoutMs: 60000 });
  }
}
