const std = @import("std");

const claudeHookSetup = @import("claude/claudeHookSetup.zig");

pub fn regHooks(init: std.process.Init, allocator: std.mem.Allocator) !void {
    std.debug.print("Building Hooks... \n", .{});

    var reg_hook_arena = std.heap.ArenaAllocator.init(allocator);
    defer reg_hook_arena.deinit();
    const reg_hook_allocator = reg_hook_arena.allocator();

    try claudeHookSetup.hookClaude(init, reg_hook_allocator);
}

pub fn clrHooks(init: std.process.Init, allocator: std.mem.Allocator) !void {
    std.debug.print("Removing hooks... \n", .{});

    var clr_hook_arena = std.heap.ArenaAllocator.init(allocator);
    defer clr_hook_arena.deinit();
    const clr_hook_allocator = clr_hook_arena.allocator();

    try claudeHookSetup.unhookClaude(init, clr_hook_allocator);
}
