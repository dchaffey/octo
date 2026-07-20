const std = @import("std");
const ClaudeHook = @import("../octoArgs.zig").OctoArgs.ClaudeHook;
const First = @import("../octoArgs.zig").OctoArgs.First;

const ClaudeTrigger = enum { Stop, SessionStart, PostToolUse };

pub fn hookClaude(init: std.process.Init, allocator: std.mem.Allocator) !void {
    const claude_dir = try openClaudeDir(init, allocator);
    defer claude_dir.close(init.io);

    var json: std.json.Value = try getClaudeSettingsJson(init.io, allocator, claude_dir);

    try ensureOctoClaudeHook(allocator, &json, ClaudeTrigger.Stop, ClaudeHook.claudeStop);
    try ensureOctoClaudeHook(allocator, &json, ClaudeTrigger.PostToolUse, ClaudeHook.claudePostToolUse);
    // std.debug.print("{f}\n", .{std.json.fmt(json, .{ .whitespace = .indent_2 })});
    try writeToClaudeSettings(init, claude_dir, json);
}

pub fn unhookClaude(init: std.process.Init, allocator: std.mem.Allocator) !void {
    const claude_dir = try openClaudeDir(init, allocator);
    defer claude_dir.close(init.io);

    var json: std.json.Value = try getClaudeSettingsJson(init.io, allocator, claude_dir);

    clearOctoClaudeHooks(&json);
    // std.debug.print("{f}\n", .{std.json.fmt(json, .{ .whitespace = .indent_2 })});
    try writeToClaudeSettings(init, claude_dir, json);
}

fn writeToClaudeSettings(init: std.process.Init, claude_dir: std.Io.Dir, json: std.json.Value) !void {
    var atomic = try claude_dir.createFileAtomic(init.io, "settings.json", .{ .replace = true });
    defer atomic.deinit(init.io);

    var write_buf: [4096]u8 = undefined;
    var fw = atomic.file.writer(init.io, &write_buf);
    try std.json.Stringify.value(json, .{ .whitespace = .indent_2 }, &fw.interface);
    try fw.interface.flush();
    try atomic.replace(init.io);
}

fn openClaudeDir(init: std.process.Init, allocator: std.mem.Allocator) !std.Io.Dir {
    const home_dir = init.environ_map.get("HOME").?;
    // std.debug.print("{s} \n", .{home_dir});
    const claude_path = try std.fmt.allocPrint(allocator, "{s}/.claude", .{home_dir});
    const claude_dir = try std.Io.Dir.cwd().openDir(init.io, claude_path, .{ .access_sub_paths = true });
    return claude_dir;
}

fn ensureOctoClaudeHook(allocator: std.mem.Allocator, settingsJson: *std.json.Value, comptime trigger: ClaudeTrigger, comptime command: ClaudeHook) !void {
    const trigger_s: [:0]const u8 = std.enums.tagName(ClaudeTrigger, trigger).?;
    const command_s: [:0]const u8 = std.enums.tagName(ClaudeHook, command).?;
    const agent_s: [:0]const u8 = std.enums.tagName(First, First.claude).?;
    const command_full: []const u8 = try std.fmt.allocPrint(allocator, "octo {s} {s}", .{ agent_s, command_s });

    if (settingsJson.object.get("hooks") == null) {
        try settingsJson.object.put(allocator, "hooks", .{ .object = .empty });
    }

    const hooks: *std.json.Value = settingsJson.object.getPtr("hooks").?;

    if (hooks.object.get(trigger_s) == null) {
        try hooks.object.put(allocator, trigger_s, .{ .array = std.json.Array.init(allocator) });
    }
    const trigger_array: *std.json.Value = hooks.object.getPtr(trigger_s).?;

    for (trigger_array.array.items) |group| {
        const inner = group.object.get("hooks") orelse continue;
        for (inner.array.items) |hook| {
            const cmd = hook.object.get("command") orelse continue;
            if (std.mem.eql(u8, command_full, cmd.string)) return;
        }
    }

    var inner_hooks = std.json.Array.init(allocator);
    var entry: std.json.ObjectMap = .empty;
    try entry.put(allocator, "type", .{ .string = "command" });
    try entry.put(allocator, "command", .{ .string = command_full });
    try inner_hooks.append(.{ .object = entry });

    var group: std.json.ObjectMap = .empty;
    try group.put(allocator, "matcher", .{ .string = "*" });
    try group.put(allocator, "hooks", .{ .array = inner_hooks });

    try trigger_array.array.append(.{ .object = group });
}

fn clearOctoClaudeHooks(settingsJson: *std.json.Value) void {
    if (settingsJson.object.get("hooks") == null) {
        return;
    }

    const hooks: *std.json.Value = settingsJson.object.getPtr("hooks").?;
    const all_triggers = std.enums.values(ClaudeTrigger);

    for (all_triggers) |trigger| {
        const trigger_s: []const u8 = std.enums.tagName(ClaudeTrigger, trigger).?;

        if (hooks.object.get(trigger_s) == null) {
            continue;
        }
        const trigger_array: *std.json.Value = hooks.object.getPtr(trigger_s).?;

        for (trigger_array.array.items) |group| {
            const inner = group.object.getPtr("hooks") orelse continue;

            var i: usize = 0;

            while (i < inner.array.items.len) {
                const cmd = inner.array.items[i].object.get("command") orelse {
                    i += 1;
                    continue;
                };
                if (std.mem.startsWith(u8, cmd.string, "octo")) {
                    _ = inner.array.orderedRemove(i);
                } else {
                    i += 1;
                }
            }
        }
    }
}

fn getClaudeSettingsJson(io: std.Io, allocator: std.mem.Allocator, claude_dir: std.Io.Dir) !std.json.Value {
    const settings_json = try claude_dir.openFile(io, "settings.json", .{ .mode = .read_only });
    defer settings_json.close(io);

    const size = try settings_json.length(io);
    const contents = try allocator.alloc(u8, size);
    var read_buf: [4096]u8 = undefined;
    var fr = settings_json.reader(io, &read_buf);
    try fr.interface.readSliceAll(contents);

    // std.debug.print("{s}\n", .{contents});

    const json = try std.json.parseFromSliceLeaky(std.json.Value, allocator, contents, .{});
    std.debug.assert(json == .object);
    return json;
}
