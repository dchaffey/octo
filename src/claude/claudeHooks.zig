const std = @import("std");
const OctoArgs = @import("../octoArgs.zig").OctoArgs;
const helpers = @import("../helpers.zig");

// Claude Code writes one JSON object to this hook's stdin per invocation; keys vary by
// hook_event_name (Stop/UserPromptSubmit/Notification/...), see readHookPayload.
pub fn handleClaudeHook(io: std.Io, allocator: std.mem.Allocator, root: []const u8, claudeHook: OctoArgs.ClaudeHook) !void {
    const socket_name = try helpers.socket_name(allocator, root);
    const addr = try std.Io.net.UnixAddress.init(socket_name);
    var stream = addr.connect(io) catch return;
    defer stream.close(io);

    var read_buf: [4096]u8 = undefined;
    var stdin_reader = std.Io.File.stdin().reader(io, &read_buf);
    const bytes = try stdin_reader.interface.allocRemaining(allocator, .unlimited);

    var wbuf: [4096]u8 = undefined;
    var w = stream.writer(io, &wbuf);
    try w.interface.writeAll(bytes);
    try w.interface.flush();

    _ = claudeHook;

    // std.debug.print("{s}\n", .{@tagName(claudeHook)});
    // std.debug.print("{f}\n", .{std.json.fmt(payload, .{ .whitespace = .indent_2 })});

    // switch (claudeHook) {
    //     OctoArgs.ClaudeHook.claudeStop => {
    //         std.debug.assert(payload.object.contains("prompt"));
    //         std.debug.assert(payload.object.contains("transcript_path"));

    //         const path = payload.object.get("prompt").?;
    //         const transcript_path = payload.object.get("transcript_path").?;

    //         std.debug.print("prompt: {s}\n", .{path.string});
    //         std.debug.print("transcript_path: {s}\n", .{transcript_path.string});
    //     },
    // }
}
