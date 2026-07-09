const std = @import("std");

const octo = @import("Octo");

pub fn main(init: std.process.Init) !void {
    std.debug.print("Starting octo...\n", .{});

    var arena = std.heap.ArenaAllocator.init(std.heap.page_allocator);
    defer arena.deinit();
    const allocator = arena.allocator();

    const octo_dir = try findOctoDir(init.io, allocator);
    _ = octo_dir;
}

fn findOctoDir(io: std.Io, allocator: std.mem.Allocator) !?std.Io.Dir {
    var root = try std.Io.Dir.cwd().openDir(io, ".", .{ .iterate = true });
    defer root.close(io);

    var walker = try root.walk(allocator);
    defer walker.deinit();

    while (try walker.next(io)) |entry| {
        std.debug.print("{s} \n", .{entry.basename});
        if (entry.kind == .directory and std.mem.eql(u8, entry.basename, ".octo")) {
            std.debug.print("Found Octo Dir", .{});

            return try entry.dir.openDir(io, entry.basename, .{});
        }
    }
    return null;
}
