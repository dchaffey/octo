const std = @import("std");
const OctoArgs = @import("../octoArgs.zig").OctoArgs;
const helpers = @import("../helpers.zig");
const agentAction = @import("../agentAction.zig");
const ClaudePostToolUse = @import("claudePostToolUseStruct.zig").ClaudePostToolUse;

const ToolUse = enum { Bash, Edit, Write, Read, WebSearch, WebFetch };

// Claude Code writes one JSON object to this hook's stdin per invocation; keys vary by
// hook_event_name (Stop/UserPromptSubmit/Notification/...), see readHookPayload.
pub fn claudeHookCall(io: std.Io, allocator: std.mem.Allocator, root: []const u8, claudeHook: OctoArgs.ClaudeHook) !void {
    const socket_name = try helpers.socket_name(allocator, root);
    const addr = try std.Io.net.UnixAddress.init(socket_name);
    var stream = addr.connect(io) catch return;
    defer stream.close(io);

    var read_buf: [4096]u8 = undefined;
    var stdin_reader = std.Io.File.stdin().reader(io, &read_buf);
    const bytes = try stdin_reader.interface.allocRemaining(allocator, .unlimited);

    var wbuf: [4096]u8 = undefined;
    var w = stream.writer(io, &wbuf);
    // prefix payload with the hook variant so readers can dispatch without parsing JSON
    try w.interface.writeAll("claude");
    try w.interface.writeAll("\n");
    try w.interface.writeAll(@tagName(claudeHook));
    try w.interface.writeAll("\n");
    try w.interface.writeAll(bytes);
    try w.interface.flush();
}

pub fn processClaudeHookPayload(
    allocator: std.mem.Allocator,
    body: []const u8,
    hook_event: OctoArgs.ClaudeHook,
    timestamp: i64,
) !?agentAction.AgentAction {
    const parsed = try std.json.parseFromSlice(std.json.Value, allocator, body, .{});
    defer parsed.deinit();

    // std.debug.print("{f}\n", .{std.json.fmt(parsed.value, .{ .whitespace = .indent_2 })});

    switch (hook_event) {
        OctoArgs.ClaudeHook.claudePostToolUse => {
            const post_tool_use = try std.json.parseFromValue(ClaudePostToolUse, allocator, parsed.value, .{ .ignore_unknown_fields = true });
            defer post_tool_use.deinit();

            const tool_use = std.meta.stringToEnum(ToolUse, post_tool_use.value.tool_name).?;

            switch (tool_use) {
                ToolUse.Edit => return try post_tool_use.value.toEditAgentAction(allocator, timestamp),
                ToolUse.Bash => return try post_tool_use.value.toBashAgentAction(allocator, timestamp),
                ToolUse.Write => return null,
                ToolUse.WebSearch => return null,
                ToolUse.WebFetch => return null,
                else => return null,
            }
        },
        OctoArgs.ClaudeHook.claudeStop => return null,
    }
}
