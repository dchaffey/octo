const std = @import("std");
const octo = @import("Octo");

const OctoRepo = @import("octoRepo.zig").OctoRepo;
const OctoArgs = @import("octoArgs.zig").OctoArgs;

const claudeHooks = @import("claude/claudeHooks.zig");
const hookSetup = @import("hookSetup.zig");
const helpers = @import("helpers.zig");

const OctoDirError = error{ NestedDirs, DuplicateDirs };

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
                try claudeHooks.handleClaudeHook(init.io, allocator, root, claudeHook);
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

    var octo_repo: OctoRepo = try OctoRepo.init(init.io, root, allocator);
    defer octo_repo.deinit();

    const socket_name = try helpers.socket_name(allocator, root);
    const addr = try std.Io.net.UnixAddress.init(socket_name);
    var server = try addr.listen(init.io, .{});
    defer server.socket.close(init.io);

    std.debug.print("Server started listening on : {s} \n", .{socket_name});

    var inbox_buf: [64][]const u8 = undefined;
    var inbox: std.Io.Queue([]const u8) = .init(&inbox_buf);

    const acceptLoopAllocator = std.heap.smp_allocator;

    var group: std.Io.Group = .init;
    group.async(init.io, acceptLoop, .{ init.io, acceptLoopAllocator, &server, &inbox });
    defer {
        group.cancel(init.io);
        _ = group.await(init.io) catch {};
    }

    // Main processing loop. We read from the inbox and process them.
    while (true) {
        const payload = inbox.getOne(init.io) catch break;
        defer acceptLoopAllocator.free(payload);
        const parsed = try std.json.parseFromSlice(std.json.Value, allocator, payload, .{});
        defer parsed.deinit();

        std.debug.print("\n{f}\n", .{std.json.fmt(parsed.value, .{ .whitespace = .indent_2 })});
    }
}

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
