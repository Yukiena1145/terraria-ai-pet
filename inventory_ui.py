"""Terraria 背包 UI 坐标与拖动。"""

from __future__ import annotations

import ctypes
import time
import win32api
import mss
import numpy as np

# Terraria 1.4.4 背包槽位实测（UIScale=1，800×600 与 1920×1009 都量过）：
# 第 1 格中心 (41, 43)，列距 47.5px，行距 47px。整体再乘 Main.UIScale。
# 之前写 50/50 会让第 10 格偏出去，写 43.6 又会把整排带偏。
TILE_SLOT_W = 47.5
TILE_SLOT_H = 47.0
HOTBAR_Y = 43.0
MAIN_ROW0_Y = 90.0
FIRST_X = 41.0


def client_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = ctypes.wintypes.RECT()
    ctypes.windll.user32.GetClientRect(int(hwnd), ctypes.byref(rect))
    point = ctypes.wintypes.POINT(0, 0)
    ctypes.windll.user32.ClientToScreen(int(hwnd), ctypes.byref(point))
    return point.x, point.y, rect.right, rect.bottom


def slot_center(
    origin_x: int,
    origin_y: int,
    width: int,
    height: int,
    slot: int,
    ui_scale: float = 1.0,
) -> tuple[int, int]:
    """1..10 为快捷栏，11..50 为主背包四行。

    UI 位置由 Terraria 的 UIScale 缩放，不按窗口分辨率拉伸；
    全屏 1920×1080 且 UIScale=1 时仍然使用同样的像素间距。
    """
    slot = int(slot)
    if not 1 <= slot <= 50:
        raise ValueError("slot 必须在 1..50")
    scale = max(0.25, min(4.0, float(ui_scale or 1.0)))
    if slot <= 10:
        x = FIRST_X + TILE_SLOT_W * (slot - 1)
        y = HOTBAR_Y
    else:
        index = slot - 11
        row, col = divmod(index, 10)
        x = FIRST_X + TILE_SLOT_W * col
        y = MAIN_ROW0_Y + TILE_SLOT_H * row
    return int(round(origin_x + x * scale)), int(round(origin_y + y * scale))


class InventoryUI:
    def __init__(self, executor, monitor, hwnd: int = 0) -> None:
        self.executor = executor
        self.monitor = monitor
        self.hwnd = int(hwnd or getattr(monitor, "_hwnd", 0) or 0)

    def _hwnd(self) -> int:
        if self.hwnd:
            return self.hwnd
        from game_window import find_window
        exe = str(getattr(self.monitor, "_exe_path", "") or "")
        window = find_window(["Terraria", "泰拉瑞亚"], exe_path=exe)
        self.hwnd = int(getattr(window, "_hWnd", 0) or 0) if window else 0
        if not self.hwnd:
            raise RuntimeError("找不到游戏窗口")
        return self.hwnd

    def _inventory_visible(self) -> bool:
        try:
            import cv2
            img, _, _ = self._client_image()
            hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
            mask = cv2.inRange(hsv, (100, 40, 40), (145, 255, 255))
            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            count = 0
            for contour in contours:
                _, _, w, h = cv2.boundingRect(contour)
                if 30 <= w <= 75 and 30 <= h <= 75:
                    count += 1
            return count >= 12
        except Exception:
            return False

    def _is_open(self) -> bool:
        """背包是否开着：优先读 bridge 的最新帧，读不到再看监控器缓存。

        关键：读的是“最新一帧”，不是构造时的旧值；否则刚打开的背包会被误判成
        “还关着”，再按一次 Esc 就又把它关上了。
        """
        try:
            fresh = self.monitor.reader.latest(max_age_s=0.8)
            if "inventory_open" in fresh:
                return bool(fresh.get("inventory_open"))
        except Exception:
            pass
        return bool(getattr(self.monitor, "inventory_open", False))

    def open(self) -> None:
        try:
            self.executor._ensure_game_focus()
        except Exception:
            pass
        # 以 bridge 状态为准；不要再用颜色检测，否则会误判后按 Esc 把背包关掉。
        if not self._is_open():
            self.executor.press_inventory()
            time.sleep(0.65)

    def force_open(self) -> None:
        """确保背包打开；已经打开时不要按 Esc——按一次是开，再按一次就关上了。"""
        try:
            self.executor._ensure_game_focus()
        except Exception:
            pass
        if self._is_open():
            return
        self.executor.press_inventory()
        time.sleep(0.65)
        try:
            self.monitor.get_state()
        except Exception:
            pass

    def force_close(self) -> None:
        """摆放世界方块前强制关掉背包；只按一次 Esc，且只在真的开着时按。"""
        try:
            self.executor._ensure_game_focus()
        except Exception:
            pass
        if not self._is_open():
            return
        self.executor.press_inventory()
        time.sleep(0.45)

    def close(self) -> None:
        """关背包：只按一次 Esc。开着箱子/商店时先走开，别连续按。"""
        try:
            self.executor._ensure_game_focus()
        except Exception:
            pass
        if not self._is_open():
            return
        self.executor.press_inventory()
        time.sleep(0.55)

    def detected_slot_centers(self) -> dict[int, tuple[int, int]]:
        """用边缘检测找背包 10×5 槽位。

        不看槽位底色（半透明、有物品挡住时会变），只看边框：任何分辨率下槽位边框
        都近似正方形。取最上面 5 行、每行最左边那一组；缺格用中位间距补出来。
        """
        try:
            self._last_detected_rows = 0
            if self.executor is not None:
                try:
                    self.executor._ensure_game_focus()
                except Exception:
                    pass
            import cv2
            import numpy as np

            img, ox, oy = self._client_image()
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            edges = cv2.Canny(gray, 40, 120)
            edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)
            contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
            boxes = []
            for contour in contours:
                x, y, w, h = cv2.boundingRect(contour)
                if not (26 <= w <= 110 and 26 <= h <= 110):
                    continue
                if not (0.72 <= w / max(1, h) <= 1.38):
                    continue
                boxes.append((x, y, w, h))
            if len(boxes) < 20:
                return {}

            heights = sorted(box[3] for box in boxes)
            median_h = heights[len(heights) // 2]
            row_tolerance = max(5.0, median_h * 0.4)

            boxes.sort(key=lambda box: box[1])
            rows: list[list[tuple[int, int, int, int]]] = []
            for box in boxes:
                for row in rows:
                    if abs(box[1] - row[0][1]) <= row_tolerance:
                        row.append(box)
                        break
                else:
                    rows.append([box])
            rows = [sorted(row, key=lambda box: box[0]) for row in rows if len(row) >= 8]
            width = img.shape[1]
            # 只认左半屏的行（心脏/装备栏在右边，别把它们的行当成背包）。
            rows = [row for row in rows if row[0][0] < width * 0.5]
            if not rows:
                return {}
            rows.sort(key=lambda row: row[0][1])
            self._last_detected_rows = len(rows)
            rows = rows[:5]

            xs = [box[0] for box in rows[0]]
            gaps = [xs[i + 1] - xs[i] for i in range(len(xs) - 1) if xs[i + 1] > xs[i]]
            if not gaps:
                return {}
            gaps_sorted = sorted(gaps)
            base = gaps_sorted[len(gaps_sorted) // 2]
            near = [gap for gap in gaps if 0.7 * base <= gap <= 1.4 * base]
            spacing = sum(near) / len(near) if near else float(base)
            if spacing < 10:
                return {}

            first = rows[0][0]
            origin_x = first[0] + first[2] / 2
            origin_y = first[1] + median_h / 2
            row_gap = spacing
            if len(rows) >= 2:
                diffs = [
                    rows[i + 1][0][1] - rows[i][0][1]
                    for i in range(min(4, len(rows) - 1))
                ]
                diffs = [diff for diff in diffs if diff > median_h * 0.6]
                if diffs:
                    row_gap = sum(diffs) / len(diffs)

            centers: dict[int, tuple[int, int]] = {}
            for slot in range(1, 51):
                col = (slot - 1) % 10
                row = (slot - 1) // 10
                centers[slot] = (
                    int(round(ox + origin_x + col * spacing)),
                    int(round(oy + origin_y + row * row_gap)),
                )
            return centers
        except Exception:
            return {}

    def center(self, slot: int) -> tuple[int, int]:
        """返回槽位中心。

        用“内存 UIScale + 实测布局”算坐标：背包是固定像素布局，只随 UIScale 缩放，
        跟窗口大小无关（800×600 和 1920×1009 实测都是同样一套比例）。
        截图检测只用来判断“格子有没有画出来”，不再参与坐标计算。
        """
        hwnd = self._hwnd()
        ox, oy, w, h = client_rect(hwnd)
        scale = float(getattr(self.monitor, "ui_scale", 1.0) or 1.0)
        return slot_center(ox, oy, w, h, slot, scale)

    def _hover_tip(self, x: int, y: int) -> str:
        """把鼠标停到某点，读游戏里的悬停 Tooltip / 物品名。"""
        try:
            import win32api

            win32api.SetCursorPos((int(x), int(y)))
        except Exception:
            return ""
        time.sleep(0.28)
        try:
            data = self.monitor.reader.latest(max_age_s=0.8)
        except Exception:
            return ""
        return str(data.get("hover_tooltip") or data.get("hover_name") or "")

    def _step_guess(self) -> tuple[int, int]:
        """估一格的水平/垂直像素间距，用于悬停搜索。"""
        centers = self.detected_slot_centers()
        if len(centers) >= 21:
            return (
                max(8, centers[2][0] - centers[1][0]),
                max(8, centers[11][1] - centers[1][1]),
            )
        scale = float(getattr(self.monitor, "ui_scale", 1.0) or 1.0)
        return (
            max(8, int(TILE_SLOT_W * scale)),
            max(8, int(TILE_SLOT_H * scale)),
        )

    def center_for_item(
        self, slot: int, name: str
    ) -> tuple[tuple[int, int], tuple[int, int]]:
        """返回 (点击坐标, 相对 center(slot) 的偏移)。

        先用 center() 算一个点，把鼠标停上去读 Tooltip；名字不对就在 ±1 格内换点
        重试。找到后把同一个偏移用到目标格上，保证两个点在同一套坐标系里。
        这样即使布局差一格，也能自己找回来。
        """
        base = self.center(slot)
        if not name:
            return base, (0, 0)
        try:
            self.executor._ensure_game_focus()
        except Exception:
            pass
        step_x, step_y = self._step_guess()
        candidates = [
            (0, 0),
            (-step_x, 0),
            (step_x, 0),
            (0, -step_y),
            (0, step_y),
            (-step_x, -step_y),
            (step_x, -step_y),
            (-step_x, step_y),
            (step_x, step_y),
        ]
        for dx, dy in candidates:
            point = (base[0] + dx, base[1] + dy)
            if name in self._hover_tip(*point):
                return point, (dx, dy)
        return base, (0, 0)

    def grid_visible(self) -> bool:
        """背包格子是不是真的画出来了。

        `Main.playerInventory` 在只开着箱子/商店时也是 True，但格子并没有画出来；
        这时候按坐标点就是乱点。这里用截图里能不能找到 4 行以上槽位来判断。
        截不了图（模拟模式/窗口丢失）时不阻止动作，交给后面的操作结果去验证。
        """
        try:
            self._client_image()
        except Exception:
            return True
        self.detected_slot_centers()
        return int(getattr(self, "_last_detected_rows", 0) or 0) >= 4

    def click_recipe(self, recipe: str = "workbench") -> None:
        """点击制作菜单里的配方图标。

        坐标来自 800×600、UIScale=1 的实测截图；全屏时按 UIScale 缩放。
        """
        recipes = {
            "workbench": (86.0, 565.0),
        }
        if recipe not in recipes:
            raise ValueError(f"未校准的配方: {recipe}")
        self.open()
        hwnd = self._hwnd()
        ox, oy, _, _ = client_rect(hwnd)
        scale = max(0.25, min(4.0, float(getattr(self.monitor, "ui_scale", 1.0) or 1.0)))
        bx, by = recipes[recipe]
        self.executor.ui_click(int(round(ox + bx * scale)), int(round(oy + by * scale)))

    def _client_image(self):
        import cv2
        hwnd = self._hwnd()
        ox, oy, w, h = client_rect(hwnd)
        with mss.mss() as sct:
            frame = np.array(sct.grab({"left": ox, "top": oy, "width": w, "height": h}))
        return cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR), ox, oy

    def highlighted_recipe_point(self) -> tuple[int, int] | None:
        """找左下制作菜单里黄色高亮的配方格。"""
        import cv2
        img, ox, oy = self._client_image()
        b, g, r = cv2.split(img)
        mask = (((r > 145) & (g > 100) & (b < 100))).astype(np.uint8) * 255
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        best = None
        best_area = 0
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            if not (28 <= w <= 110 and 28 <= h <= 110):
                continue
            if x > 350 or y < 250:
                continue
            area = w * h
            if area > best_area:
                best_area = area
                best = (x, y, w, h)
        if best is None:
            return None
        x, y, w, h = best
        return int(ox + x + w / 2), int(oy + y + h / 2)

    def _tip_at(self, x: int, y: int) -> str:
        win32api.SetCursorPos((int(x), int(y)))
        time.sleep(0.20)
        try:
            data = self.monitor.reader.latest(max_age_s=0.6)
        except Exception:
            data = {}
        return str(data.get("hover_tooltip") or "")

    def scroll_to_recipe(self, target: str) -> tuple[int, int] | None:
        """悬停左下制作区，慢速滚动直到 Tooltip 出现目标物品。"""
        self.open()
        target = str(target or "").strip()
        if not target:
            return None
        point = self.highlighted_recipe_point()
        if point is None:
            hwnd = self._hwnd()
            ox, oy, _, h = client_rect(hwnd)
            scale = max(0.25, min(4.0, float(getattr(self.monitor, "ui_scale", 1.0) or 1.0)))
            # 制作栏贴左下，按客户端高度估算；识别不到高亮格时的兜底。
            point = (int(round(ox + 50 * scale)), int(round(oy + h - 365 * scale)))
        x, y = point
        # 最多找 12 秒：以前两个方向各滚 80 次、每次 0.35s，最坏要 56 秒，
        # 插件动作会超时，看起来就像“卡住/找不到配方”。
        deadline = time.monotonic() + 12.0
        for direction in (-1, 1):
            unchanged = 0
            for _ in range(80):
                if time.monotonic() > deadline:
                    return None
                tip = self._tip_at(x, y)
                if target in tip:
                    return x, y
                self.executor.scroll_ui(direction)
                time.sleep(0.30)
                new_tip = self._tip_at(x, y)
                if new_tip == tip:
                    unchanged += 1
                    if unchanged >= 4:
                        break
                else:
                    unchanged = 0
        return None

    def craft_button_point(self, recipe_point: tuple[int, int]) -> tuple[int, int]:
        scale = max(0.25, min(4.0, float(getattr(self.monitor, "ui_scale", 1.0) or 1.0)))
        return int(round(recipe_point[0] + 49 * scale)), int(round(recipe_point[1] - 5 * scale))

    def ensure_hotbar_item(self, name: str) -> int | None:
        """把指定物品放进快捷栏并返回槽位。"""
        target = str(name or "")
        inv = self.monitor.inventory
        item = next(
            (i for i in inv
             if str(i.get("name", "")) == target
             and 1 <= int(i.get("slot", 0) or 0) <= 10),
            None,
        )
        if item is None:
            item = next((i for i in inv if str(i.get("name", "")) == target), None)
        if item is None:
            return None
        slot = int(item.get("slot", 0) or 0)
        if 1 <= slot <= 10:
            return slot
        self.open()
        # 先找空快捷栏；没有就换掉第 9 格（通常是杂物）。
        used = {int(i.get("slot", 0)) for i in inv if i.get("hotbar")}
        dest = next((s for s in range(1, 11) if s not in used), 1)
        self.move_item(slot, dest)
        self.monitor.get_state()
        # 点击移动失败时再试一次拖动。
        moved = next((i for i in self.monitor.inventory if str(i.get("name", "")) == target), None)
        if moved is None or int(moved.get("slot", 0)) > 10:
            fx, fy = self.center(slot)
            tx, ty = self.center(dest)
            self.executor.ui_drag(fx, fy, tx, ty)
            time.sleep(0.35)
            self.monitor.get_state()
            moved = next((i for i in self.monitor.inventory if str(i.get("name", "")) == target), None)
        if moved is None or int(moved.get("slot", 0)) > 10:
            return None
        return int(moved.get("slot", dest))

    def empty_inventory_slot(self) -> int | None:
        # monitor.inventory 只包含非空格；空格要按 1..50 反推。
        used = {
            int(item.get("slot", 0) or 0)
            for item in self.monitor.inventory
            if int(item.get("stack", 0) or 0) > 0
        }
        for slot in range(1, 51):
            if slot not in used:
                return slot
        return None

    def move_item(self, from_slot: int, to_slot: int) -> None:
        """Terraria 的移动方式：点一下物品，再点目标格。"""
        self.open()
        fx, fy = self.center(from_slot)
        tx, ty = self.center(to_slot)
        self.executor.ui_click(fx, fy)
        time.sleep(0.30)
        self.executor.ui_click(tx, ty)
        time.sleep(0.30)
        # 目标格原本有物品时，第二次点击会把它换到光标上；
        # 再点回源格，保证不会把物品丢在光标上。
        self.executor.ui_click(fx, fy)
        time.sleep(0.30)
