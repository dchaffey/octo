const std = @import("std");
const octo = @import("Octo");

const OctoRepo = @import("octoRepo.zig").OctoRepo;
const OctoArgs = @import("octoArgs.zig").OctoArgs;
const UiMsg = @import("uiMsg.zig").UiMsg;

const claudeHooks = @import("claude/claudeHooks.zig");
const hookSetup = @import("hookSetup.zig");
const helpers = @import("helpers.zig");
const tui = @import("vaxis/tui.zig");

const OctoError = error{NotAnAgent};

pub fn main(init: std.process.Init) !void {
    var arena = std.heap.ArenaAllocator.init(std.heap.page_allocator);
    defer arena.deinit();
    const allocator = arena.allocator();

    const args: []const [:0]const u8 = try init.minimal.args.toSlice(allocator);
    const root = try std.process.currentPathAlloc(init.io, allocator);

    if (args.len != 1) {
        const first: OctoArgs.First = std.meta.stringToEnum(OctoArgs.First, args[1]).?;
        switch (first) {
            OctoArgs.First.claude => {
                const claudeHook: OctoArgs.ClaudeHook = std.meta.stringToEnum(OctoArgs.ClaudeHook, args[2]).?;
                try claudeHooks.claudeHookCall(init.io, allocator, root, claudeHook);
            },
            OctoArgs.First.uninstall => try hookSetup.clrHooks(init, allocator),
        }
        return;
    }

    // Main Loop

    std.debug.print("Starting octo...\n", .{});
    std.debug.print("Checking whether hooks are installed. \n", .{});

    hookSetup.regHooks(init, allocator) catch |err| {
        std.debug.print("FAILED to install hooks for agents. Error: {} \n Aborting. \n", .{err});
        return;
    };

    // ------------- OctoRepo setup -------------
    var octo_repo: OctoRepo = try OctoRepo.init(init.io, root, allocator);
    defer octo_repo.deinit();

    var group: std.Io.Group = .init;

    // ------------- TUI thread -------------
    var tui_inbox_buf: [64]UiMsg = undefined;
    var tui_inbox: std.Io.Queue(UiMsg) = .init(&tui_inbox_buf);
    group.async(init.io, tuiRun, .{ init, &tui_inbox });

    // ------------- Hook socket -------------
    const socket_name = try helpers.socket_name(allocator, root);
    const addr = try std.Io.net.UnixAddress.init(socket_name);
    var server = try addr.listen(init.io, .{});
    defer server.socket.close(init.io);

    std.debug.print("Server started listening on : {s} \n", .{socket_name});

    // ------------- Hook thread -------------
    const acceptLoopAllocator = std.heap.smp_allocator;
    var hook_inbox_buf: [64][]const u8 = undefined;
    var hook_inbox: std.Io.Queue([]const u8) = .init(&hook_inbox_buf);
    group.async(init.io, acceptLoop, .{ init.io, acceptLoopAllocator, &server, &hook_inbox });
    defer {
        group.cancel(init.io);
        _ = group.await(init.io) catch {};
    }

    // Main processing loop. We read from the inbox and process them.
    while (true) {
        const payload = hook_inbox.getOne(init.io) catch break;
        defer acceptLoopAllocator.free(payload);

        // payload is "<agent>\n<ClaudeHook tag>\n<json body>"; split off both prefixes before parsing
        // ------------- Agent name parsing -------------
        const newline_idx1 = std.mem.indexOfScalar(u8, payload, '\n').?;
        const agent = payload[0..newline_idx1];
        const agent_id = std.meta.stringToEnum(OctoArgs.First, agent).?;

        // ------------- Hook name parsing -------------
        const rest = payload[newline_idx1 + 1 ..];
        const newline_idx2 = std.mem.indexOfScalar(u8, rest, '\n').?;
        const hook_tag = rest[0..newline_idx2];
        const body: []const u8 = rest[newline_idx2 + 1 ..];

        // std.debug.print("\n[{s}]\n[{s}]\n", .{ agent, hook_tag });

        const timestamp = std.Io.Clock.now(.awake, init.io);

        switch (agent_id) {
            OctoArgs.First.claude => {
                const hook_event = std.meta.stringToEnum(OctoArgs.ClaudeHook, hook_tag).?;
                _ = try claudeHooks.processClaudeHookPayload(allocator, body, hook_event, timestamp.toMilliseconds());
            },
            else => return OctoError.NotAnAgent,
        }
    }
}

// Runs the TUI on the group's async task; group.async requires a void-returning callback, so errors are logged here rather than propagated.
fn tuiRun(init: std.process.Init, tui_inbox: *std.Io.Queue(UiMsg)) void {
    tui.tuiMain(init, tui_inbox) catch |err| {
        std.debug.print("TUI exited with error: {} \n", .{err});
    };
}

// This loop constantly listens to data coming in through the socket and forwards this to main loop with the queue.
fn acceptLoop(io: std.Io, allocator: std.mem.Allocator, server: *std.Io.net.Server, inbox: *std.Io.Queue([]const u8)) void {
    while (true) {
        var stream = server.accept(io) catch return;
        defer stream.close(io);
        var rbuf: [4096]u8 = undefined;
        var reader = stream.reader(io, &rbuf);
        const payload = reader.interface.allocRemaining(allocator, .limited(1 << 20)) catch continue;
        inbox.putOne(io, payload) catch return;
    }
}
