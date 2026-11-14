using System;
using System.Globalization;
using System.Text;
using System.Threading;
using Microsoft.Diagnostics.Runtime;

class Program
{
    static DataTarget dt;
    static ClrHeap heap;
    static ClrAppDomain domain;
    static ClrStaticField playerField;
    static ClrStaticField screenField;
    static ClrStaticField menuModeField;
    static ClrStaticField playerInventoryField;
    static ClrStaticField uiScaleField;
    static ClrStaticField hoverItemNameField;
    static ClrStaticField hoverItemField;
    static ClrStaticField mouseItemField;
    static ClrStaticField mouseTextLinesField;
    static ClrType itemNameArrayType;
    static ulong itemNameArrayAddress;
    static ClrStaticField tileField;
    static ClrStaticField npcField;
    static ClrStaticField maxTilesXField;
    static ClrStaticField maxTilesYField;
    static string lastTileJson = "null";
    static bool tilesEnabled = false;
    static bool tileDebugPrinted = false;
    static long lastTileMs = 0;

    static float ReadFloat(ulong address)
    {
        byte[] buf = new byte[4];
        int read;
        dt.ReadProcessMemory(address, buf, 4, out read);
        return BitConverter.ToSingle(buf, 0);
    }

    static uint ReadUInt(ulong address)
    {
        byte[] buf = new byte[4];
        int read;
        dt.ReadProcessMemory(address, buf, 4, out read);
        return BitConverter.ToUInt32(buf, 0);
    }

    static int ReadInt(ulong address)
    {
        byte[] buf = new byte[4];
        int read;
        dt.ReadProcessMemory(address, buf, 4, out read);
        return BitConverter.ToInt32(buf, 0);
    }

    static void Emit(string json)
    {
        Console.WriteLine(json);
        Console.Out.Flush();
    }

    static string DumpTiles(float playerX, float playerY)
    {
        try
        {
            if (tileField == null || maxTilesXField == null)
                return "null";
            object raw = tileField.GetValue(domain);
            if (raw == null)
                return "null";
            ulong arrayAddress = Convert.ToUInt64(raw);
            ClrType arrayType = heap.GetObjectType(arrayAddress);
            if (arrayType == null) return "null";
            // The runtime array header is authoritative; Main.maxTilesX can be
            // a half-size/logical value in some builds.
            int width = ReadInt(arrayAddress + 8);
            int height = ReadInt(arrayAddress + 12);
            ClrType tileType = arrayType.ComponentType;
            if (tileType == null) return "null";
            ClrInstanceField typeField = tileType.GetFieldByName("type");
            ClrInstanceField wallField = tileType.GetFieldByName("wall");
            ClrInstanceField headerField = tileType.GetFieldByName("sTileHeader");
            if (typeField == null || wallField == null || headerField == null)
                return "{\"error\":\"Tile fields missing\"}";

            if (!tileDebugPrinted)
            {
                tileDebugPrinted = true;
                try
                {
                    ulong e0 = arrayType.GetArrayElementAddress(arrayAddress, 0);
                    Console.Error.WriteLine(string.Format(CultureInfo.InvariantCulture,
                        "TILE_DEBUG width={0} height={1} elem={2} typeOff={3} wallOff={4} headerOff={5} arr=0x{6:X} e0=0x{7:X} delta={8} len={9} base={10} valueType={11} name={12} hdr={13}",
                        width, height, arrayType.ElementSize, typeField.Offset, wallField.Offset, headerField.Offset,
                        arrayAddress, e0, (long)(e0 - arrayAddress), arrayType.GetArrayLength(arrayAddress), arrayType.BaseSize,
                        tileType.IsValueClass, tileType.Name,
                        string.Join(",", new int[]{ReadInt(arrayAddress+8),ReadInt(arrayAddress+12),ReadInt(arrayAddress+16),ReadInt(arrayAddress+20),ReadInt(arrayAddress+24),ReadInt(arrayAddress+28),ReadInt(arrayAddress+32),ReadInt(arrayAddress+36)})));
                }
                catch (Exception ex) { Console.Error.WriteLine("TILE_DEBUG_ERR " + ex.Message); }
            }

            int cx = (int)Math.Floor((playerX + 10.0f) / 16.0f);
            int cy = (int)Math.Floor((playerY + 21.0f) / 16.0f);
            StringBuilder sb = new StringBuilder(8192);
            sb.Append('[');
            for (int dy = -15; dy <= 15; dy++)
            {
                if (dy > -15) sb.Append(',');
                sb.Append('[');
                for (int dx = -15; dx <= 15; dx++)
                {
                    if (dx > -15) sb.Append(',');
                    int wx = cx + dx, wy = cy + dy;
                    if (wx < 0 || wy < 0 || wx >= width || wy >= height)
                    {
                        sb.Append("{\"has_tile\":false,\"tile_type\":0,\"has_wall\":false,\"wall_type\":0}");
                        continue;
                    }
                    try
                    {
                        // Terraria.Tile[,] is [x, y]; CLR row-major = x * height + y
                        int flat = wx * height + wy;
                        ulong elem = arrayAddress + 24 + (ulong)flat * 4UL;
                        uint ptr = ReadUInt(elem);
                        if (ptr == 0)
                        {
                            sb.Append("{\"has_tile\":false,\"tile_type\":0,\"has_wall\":false,\"wall_type\":0}");
                            continue;
                        }
                        ulong obj = ptr;
                        int type = Convert.ToInt32(typeField.GetValue(obj, false));
                        int wall = Convert.ToInt32(wallField.GetValue(obj, false));
                        int header = Convert.ToInt32(headerField.GetValue(obj, false));
                        bool hasTile = (header & 0x20) != 0;
                        bool hasWall = wall > 0;
                        sb.Append("{\"has_tile\":");
                        sb.Append(hasTile ? "true" : "false");
                        sb.Append(",\"tile_type\":").Append(type);
                        sb.Append(",\"has_wall\":");
                        sb.Append(hasWall ? "true" : "false");
                        sb.Append(",\"wall_type\":").Append(wall);
                        sb.Append('}');
                    }
                    catch
                    {
                        sb.Append("{\"has_tile\":false,\"tile_type\":0,\"has_wall\":false,\"wall_type\":0}");
                    }
                }
                sb.Append(']');
            }
            sb.Append(']');
            return sb.ToString();
        }
        catch (Exception ex)
        {
            string msg = ex.Message.Replace("\\", "/").Replace("\"", "'");
            return "{\"error\":\"" + msg + "\"}";
        }
    }

    static string ReadNetString(ulong address)
    {
        try
        {
            ClrType t = heap.GetObjectType(address);
            if (t == null) return "";
            object v = t.GetValue(address);
            return v as string ?? "";
        }
        catch { return ""; }
    }

    static string AsString(object value)
    {
        if (value is string) return (string)value;
        try
        {
            if (value != null)
            {
                ulong addr = Convert.ToUInt64(value);
                if (addr != 0) return ReadNetString(addr);
            }
        }
        catch { }
        return "";
    }

    static string ItemName(int id)
    {
        try
        {
            if (itemNameArrayType == null || itemNameArrayAddress == 0 || id < 0) return "";
            int length = itemNameArrayType.GetArrayLength(itemNameArrayAddress);
            if (id >= length) return "";
            ulong elem = itemNameArrayType.GetArrayElementAddress(itemNameArrayAddress, id);
            uint ptr = ReadUInt(elem);
            if (ptr == 0) return "";
            ClrType lt = heap.GetObjectType(ptr);
            if (lt == null) return "";
            ClrInstanceField valueField = lt.GetFieldByName("_value");
            string text = valueField == null ? "" : AsString(valueField.GetValue(ptr, false));
            if (!string.IsNullOrEmpty(text)) return text;
            ClrInstanceField english = lt.GetFieldByName("<EnglishValue>k__BackingField");
            return english == null ? "" : AsString(english.GetValue(ptr, false));
        }
        catch { return ""; }
    }

    static string DumpInventory(ClrType playerType, ulong playerAddress)
    {
        try
        {
            ClrInstanceField invField = playerType.GetFieldByName("inventory");
            ClrInstanceField selField = playerType.GetFieldByName("selectedItem");
            if (invField == null) return "[]";
            ulong selected = 0;
            if (selField != null)
            {
                try { selected = (ulong)ReadInt(selField.GetAddress(playerAddress)); } catch { }
            }
            object raw = invField.GetValue(playerAddress, false);
            if (raw == null) return "[]";
            ulong arrayAddress = Convert.ToUInt64(raw);
            ClrType arrayType = heap.GetObjectType(arrayAddress);
            if (arrayType == null) return "[]";
            int length = Math.Min(50, arrayType.GetArrayLength(arrayAddress));
            StringBuilder sb = new StringBuilder(1024);
            sb.Append('[');
            for (int i = 0; i < length; i++)
            {
                if (i > 0) sb.Append(',');
                try
                {
                    ulong slot = arrayType.GetArrayElementAddress(arrayAddress, i);
                    uint ptr = ReadUInt(slot);
                    if (ptr == 0)
                    {
                        sb.Append("null");
                        continue;
                    }
                    ClrType itemType = heap.GetObjectType(ptr);
                    int type = 0, stack = 0, axe = 0, pick = 0, hammer = 0, damage = 0, createTile = -1, createWall = -1;
                    ClrInstanceField f;
                    f = itemType.GetFieldByName("type"); if (f != null) type = Convert.ToInt32(f.GetValue(ptr, false));
                    f = itemType.GetFieldByName("stack"); if (f != null) stack = Convert.ToInt32(f.GetValue(ptr, false));
                    f = itemType.GetFieldByName("axe"); if (f != null) axe = Convert.ToInt32(f.GetValue(ptr, false));
                    // 泰拉瑞亚 1.4.4 的镐力字段叫 pick；旧版/兼容名才是 pickaxe。
                    f = itemType.GetFieldByName("pick"); if (f != null) pick = Convert.ToInt32(f.GetValue(ptr, false));
                    if (pick == 0)
                    {
                        f = itemType.GetFieldByName("pickaxe"); if (f != null) pick = Convert.ToInt32(f.GetValue(ptr, false));
                    }
                    f = itemType.GetFieldByName("hammer"); if (f != null) hammer = Convert.ToInt32(f.GetValue(ptr, false));
                    f = itemType.GetFieldByName("damage"); if (f != null) damage = Convert.ToInt32(f.GetValue(ptr, false));
                    // createTile / createWall 决定这件物品能不能放成方块/墙（-1 = 不能）
                    f = itemType.GetFieldByName("createTile"); if (f != null) createTile = Convert.ToInt32(f.GetValue(ptr, false));
                    f = itemType.GetFieldByName("createWall"); if (f != null) createWall = Convert.ToInt32(f.GetValue(ptr, false));
                    sb.Append("{\"slot\":").Append(i + 1);
                    sb.Append(",\"type\":").Append(type);
                    sb.Append(",\"stack\":").Append(stack);
                    sb.Append(",\"name\":\"").Append(ItemName(type).Replace("\\", "/").Replace("\"", "'")).Append("\"");
                    sb.Append(",\"axe\":").Append(axe);
                    sb.Append(",\"pickaxe\":").Append(pick);
                    sb.Append(",\"hammer\":").Append(hammer);
                    sb.Append(",\"damage\":").Append(damage);
                    sb.Append(",\"create_tile\":").Append(createTile);
                    sb.Append(",\"create_wall\":").Append(createWall);
                    sb.Append('}');
                }
                catch { sb.Append("null"); }
            }
            sb.Append(']');
            return sb.ToString();
        }
        catch { return "[]"; }
    }

    static string DumpEnemies()
    {
        try
        {
            if (npcField == null) return "[]";
            object raw = npcField.GetValue(domain);
            if (raw == null) return "[]";
            ulong arr = Convert.ToUInt64(raw);
            ClrType at = heap.GetObjectType(arr);
            if (at == null) return "[]";
            int length = Math.Min(200, at.GetArrayLength(arr));
            StringBuilder sb = new StringBuilder(2048);
            sb.Append('[');
            bool first = true;
            for (int i = 0; i < length; i++)
            {
                try
                {
                    uint ptr = ReadUInt(at.GetArrayElementAddress(arr, i));
                    if (ptr == 0) continue;
                    ClrType nt = heap.GetObjectType(ptr);
                    if (nt == null) continue;
                    ClrInstanceField activeF = nt.GetFieldByName("active");
                    if (activeF == null || Convert.ToInt32(activeF.GetValue(ptr, false)) == 0) continue;
                    ClrInstanceField friendlyF = nt.GetFieldByName("friendly");
                    ClrInstanceField townF = nt.GetFieldByName("townNPC");
                    bool friendly = friendlyF != null && Convert.ToInt32(friendlyF.GetValue(ptr, false)) != 0;
                    bool town = townF != null && Convert.ToInt32(townF.GetValue(ptr, false)) != 0;
                    if (friendly || town) continue;
                    ClrInstanceField posF = nt.GetFieldByName("position");
                    ClrInstanceField wF = nt.GetFieldByName("width");
                    ClrInstanceField hF = nt.GetFieldByName("height");
                    ClrInstanceField lifeF = nt.GetFieldByName("life");
                    ClrInstanceField typeF = nt.GetFieldByName("type");
                    if (posF == null) continue;
                    ulong pa = posF.GetAddress(ptr);
                    float x = ReadFloat(pa), y = ReadFloat(pa + 4);
                    int w = wF == null ? 16 : Convert.ToInt32(wF.GetValue(ptr, false));
                    int h = hF == null ? 16 : Convert.ToInt32(hF.GetValue(ptr, false));
                    int life = lifeF == null ? 0 : Convert.ToInt32(lifeF.GetValue(ptr, false));
                    int typ = typeF == null ? 0 : Convert.ToInt32(typeF.GetValue(ptr, false));
                    if (!first) sb.Append(',');
                    first = false;
                    sb.Append("{\"i\":").Append(i);
                    sb.Append(",\"x\":").Append(x).Append(",\"y\":").Append(y);
                    sb.Append(",\"w\":").Append(w).Append(",\"h\":").Append(h);
                    sb.Append(",\"life\":").Append(life).Append(",\"type\":").Append(typ);
                    sb.Append('}');
                }
                catch { }
            }
            sb.Append(']');
            return sb.ToString();
        }
        catch { return "[]"; }
    }

    static string DumpItemCatalog()
    {
        try
        {
            if (itemNameArrayType == null || itemNameArrayAddress == 0) return "[]";
            int n = itemNameArrayType.GetArrayLength(itemNameArrayAddress);
            StringBuilder sb = new StringBuilder(300000);
            sb.Append('[');
            for (int i = 0; i < n; i++)
            {
                string name = ItemName(i);
                if (string.IsNullOrEmpty(name) || name == "ItemName.None") continue;
                if (sb.Length > 1) sb.Append(',');
                sb.Append("{\"id\":").Append(i).Append(",\"name\":\"")
                  .Append(name.Replace("\\", "/").Replace("\"", "'")).Append("\"}");
            }
            sb.Append(']');
            return sb.ToString();
        }
        catch (Exception ex) { return "{\"error\":\"" + ex.Message.Replace("\"","'") + "\"}"; }
    }

    static void Dump()
    {
        try
        {
            object raw = playerField.GetValue(domain);
            if (raw == null) { Emit("{\"ok\":false,\"error\":\"Main.player is null\"}"); return; }
            ulong arrayAddress = Convert.ToUInt64(raw);
            ClrType arrayType = heap.GetObjectType(arrayAddress);
            if (arrayType == null || arrayType.GetArrayLength(arrayAddress) <= 0)
            {
                Emit("{\"ok\":false,\"error\":\"player array empty\"}");
                return;
            }
            ulong playerAddress = Convert.ToUInt64(arrayType.GetArrayElementValue(arrayAddress, 0));
            ClrType playerType = heap.GetObjectType(playerAddress);
            if (playerType == null) { Emit("{\"ok\":false,\"error\":\"player null\"}"); return; }

            ClrInstanceField pos = playerType.GetFieldByName("position");
            ClrInstanceField vel = playerType.GetFieldByName("velocity");
            ClrInstanceField hp = playerType.GetFieldByName("statLife");
            ClrInstanceField maxHp = playerType.GetFieldByName("statLifeMax");
            if (pos == null || vel == null)
            {
                Emit("{\"ok\":false,\"error\":\"position/velocity fields missing\"}");
                return;
            }

            ulong posAddr = pos.GetAddress(playerAddress);
            ulong velAddr = vel.GetAddress(playerAddress);
            float x = ReadFloat(posAddr);
            float y = ReadFloat(posAddr + 4);
            float vx = ReadFloat(velAddr);
            float vy = ReadFloat(velAddr + 4);
            int life = hp == null ? -1 : ReadInt(hp.GetAddress(playerAddress));
            int maxLife = maxHp == null ? -1 : ReadInt(maxHp.GetAddress(playerAddress));

            float sx = 0, sy = 0;
            if (screenField != null)
            {
                ulong screenAddr = screenField.GetAddress(domain);
                try
                {
                    object sv = screenField.GetValue(domain);
                    if (sv != null)
                    {
                        // Static Vector2 comes back as the address of the boxed value.
                        screenAddr = Convert.ToUInt64(sv);
                    }
                }
                catch { }
                sx = ReadFloat(screenAddr + 4);
                sy = ReadFloat(screenAddr + 8);
            }

            bool inventoryOpen = false;
            if (playerInventoryField != null)
            {
                try
                {
                    // Main.playerInventory 是 1 字节的 bool。以前用 ReadInt 读 4 字节，
                    // 会把相邻静态字段的字节一起读进来，导致背包明明关着也常读成 true。
                    object invValue = playerInventoryField.GetValue(domain);
                    if (invValue is bool) inventoryOpen = (bool)invValue;
                    else if (invValue != null) inventoryOpen = Convert.ToInt32(invValue) != 0;
                }
                catch { }
            }

            string hoverName = "";
            if (hoverItemNameField != null)
            {
                try
                {
                    object hv = hoverItemNameField.GetValue(domain);
                    if (hv is string) hoverName = (string)hv;
                    if (string.IsNullOrEmpty(hoverName))
                    {
                        uint hptr = ReadUInt(hoverItemNameField.GetAddress(domain));
                        if (hptr != 0) hoverName = ReadNetString(hptr);
                    }
                }
                catch { }
            }
            string hoverTooltip = "";
            if (mouseTextLinesField != null)
            {
                try
                {
                    object mv = mouseTextLinesField.GetValue(domain);
                    if (mv != null)
                    {
                        ulong ma = Convert.ToUInt64(mv);
                        ClrType mt = heap.GetObjectType(ma);
                        if (mt != null)
                        {
                            int ml = mt.GetArrayLength(ma);
                            for (int mi = 0; mi < Math.Min(8, ml); mi++)
                            {
                                uint mp = ReadUInt(mt.GetArrayElementAddress(ma, mi));
                                if (mp != 0)
                                {
                                    string ms = ReadNetString(mp);
                                    if (!string.IsNullOrEmpty(ms)) { hoverTooltip = ms; break; }
                                }
                            }
                        }
                    }
                }
                catch { }
            }
            string mouseItemName = "";
            if (mouseItemField != null)
            {
                try
                {
                    object mv2 = mouseItemField.GetValue(domain);
                    if (mv2 != null)
                    {
                        ulong mp2 = Convert.ToUInt64(mv2);
                        ClrType mt2 = heap.GetObjectType(mp2);
                        ClrInstanceField tf2 = mt2 == null ? null : mt2.GetFieldByName("type");
                        if (tf2 != null)
                            mouseItemName = ItemName(Convert.ToInt32(tf2.GetValue(mp2, false)));
                    }
                }
                catch { }
            }
            string hoverItemName = "";
            if (hoverItemField != null)
            {
                try
                {
                    object hv = hoverItemField.GetValue(domain);
                    if (hv != null)
                    {
                        ulong hoverPtr = Convert.ToUInt64(hv);
                        ClrType ht = heap.GetObjectType(hoverPtr);
                        ClrInstanceField tf = ht == null ? null : ht.GetFieldByName("type");
                        if (tf != null)
                        {
                            int tid = Convert.ToInt32(tf.GetValue(hoverPtr, false));
                            hoverItemName = ItemName(tid);
                        }
                    }
                }
                catch { }
            }
            if (string.IsNullOrEmpty(hoverItemName)) hoverItemName = hoverName;
            float uiScale = 1.0f;
            if (uiScaleField != null)
            {
                try { uiScale = ReadFloat(uiScaleField.GetAddress(domain)); } catch { }
            }

            int menuMode = -1;
            if (menuModeField != null)
            {
                try { menuMode = ReadInt(menuModeField.GetAddress(domain)); } catch { }
            }

            int selected = 0;
            try
            {
                ClrInstanceField selField = playerType.GetFieldByName("selectedItem");
                if (selField != null) selected = ReadInt(selField.GetAddress(playerAddress)) + 1;
            }
            catch { }
            string baseJson = string.Format(CultureInfo.InvariantCulture,
                "{{\"ok\":true,\"x\":{0},\"y\":{1},\"vx\":{2},\"vy\":{3},\"hp\":{4},\"max_hp\":{5},\"screen_x\":{6},\"screen_y\":{7},\"menu_mode\":{8},\"player_ptr\":{9},\"selected_slot\":{10},\"inventory\":{11}",
                x, y, vx, vy, life, maxLife, sx, sy, menuMode, playerAddress, selected, DumpInventory(playerType, playerAddress));
            baseJson += ",\"inventory_open\":" + (inventoryOpen ? "true" : "false");
            baseJson += string.Format(CultureInfo.InvariantCulture, ",\"ui_scale\":{0}", uiScale);
            baseJson += ",\"hover_name\":\"" + hoverItemName.Replace("\\", "/").Replace("\"", "'") + "\"";
            baseJson += ",\"hover_tooltip\":\"" + hoverTooltip.Replace("\\", "/").Replace("\"", "'") + "\"";
            baseJson += ",\"mouse_name\":\"" + mouseItemName.Replace("\\", "/").Replace("\"", "'") + "\"";
            baseJson += ",\"enemies\":" + DumpEnemies();
            if (tilesEnabled)
            {
                long nowMs = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
                if (nowMs - lastTileMs >= 200)
                {
                    lastTileMs = nowMs;
                    lastTileJson = DumpTiles(x, y);
                }
            }
            Emit(baseJson + ",\"tiles\":" + lastTileJson + "}");
        }
        catch (Exception ex)
        {
            string msg = ex.Message.Replace("\\", "/").Replace("\"", "'");
            Emit("{\"ok\":false,\"error\":\"" + msg + "\"}");
        }
    }

    static void Main(string[] args)
    {
        Console.OutputEncoding = new UTF8Encoding(false);
        int pid = args.Length > 0 ? int.Parse(args[0]) : 0;
        bool watch = args.Length > 1 && args[1].ToLowerInvariant() == "watch";
        int interval = args.Length > 2 ? int.Parse(args[2]) : 100;
        tilesEnabled = Array.Exists(args, a => a.ToLowerInvariant() == "tiles");
        bool itemDump = Array.Exists(args, a => a.ToLowerInvariant() == "itemdump");

        dt = DataTarget.AttachToProcess(pid, 15000, AttachFlag.Passive);
        ClrInfo info = dt.ClrVersions[0];
        ClrRuntime runtime = info.CreateRuntime();
        heap = runtime.Heap;
        ClrType main = heap.GetTypeByName("Terraria.Main");
        if (main == null) { Emit("{\"ok\":false,\"error\":\"Terraria.Main not found\"}"); return; }
        playerField = main.GetStaticFieldByName("player");
        screenField = main.GetStaticFieldByName("screenPosition");
        menuModeField = main.GetStaticFieldByName("menuMode");
        playerInventoryField = main.GetStaticFieldByName("playerInventory");
        uiScaleField = main.GetStaticFieldByName("UIScale");
        hoverItemNameField = main.GetStaticFieldByName("hoverItemName");
        hoverItemField = main.GetStaticFieldByName("HoverItem");
        mouseItemField = main.GetStaticFieldByName("mouseItem");
        mouseTextLinesField = main.GetStaticFieldByName("_mouseTextTooltipLine_Text");
        tileField = main.GetStaticFieldByName("tile");
        npcField = main.GetStaticFieldByName("npc");
        maxTilesXField = main.GetStaticFieldByName("maxTilesX");
        maxTilesYField = main.GetStaticFieldByName("maxTilesY");
        if (playerField == null) { Emit("{\"ok\":false,\"error\":\"Main.player not found\"}"); return; }
        domain = runtime.AppDomains[0];
        try
        {
            ClrType lang2 = heap.GetTypeByName("Terraria.Lang");
            ClrStaticField namesField = lang2 == null ? null : lang2.GetStaticFieldByName("_itemNameCache");
            if (namesField != null)
            {
                object rawNames = namesField.GetValue(domain);
                if (rawNames != null)
                {
                    itemNameArrayAddress = Convert.ToUInt64(rawNames);
                    itemNameArrayType = heap.GetObjectType(itemNameArrayAddress);
                }
            }
        }
        catch (Exception nex) { Console.Error.WriteLine("ITEM_NAME_ERR " + nex.Message); }

        if (itemDump)
        {
            Emit(DumpItemCatalog());
            return;
        }
        if (!watch)
        {
            Dump();
            return;
        }

        while (true)
        {
            Dump();
            Thread.Sleep(Math.Max(20, interval));
        }
    }
}
