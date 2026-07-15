const std = @import("std");

const octo = @import("Octo");
const OctoRepo = @import("octoRepo.zig").OctoRepo;

const OctoDirError = error{ NestedDirs, DuplicateDirs };

pub fn main(init: std.process.Init) !void {
    std.debug.print("Starting octo...\n", .{});

    var arena = std.heap.ArenaAllocator.init(std.heap.page_allocator);
    defer arena.deinit();
    const allocator = arena.allocator();

    var octo_repo: OctoRepo = try OctoRepo.init(init.io, allocator);
    defer octo_repo.deinit();
}

fn findOctoDir(io: std.Io, allocator: std.mem.Allocator) !std.Io.Dir {
    var root = try std.Io.Dir.cwd().openDir(io, ".", .{ .iterate = true });
    defer root.close(io);

    var walker = try root.walk(allocator);
    defer walker.deinit();

    var octoDir: ?std.Io.Dir = null;

    std.debug.print("Exitsting .octo dirs: \n\n", .{});
    while (try walker.next(io)) |entry| {
        if (entry.kind == .directory and std.mem.eql(u8, entry.path, ".octo")) {
            if (octoDir != null) {
                return OctoDirError.DuplicateDirs;
            }
            octoDir = try entry.dir.openDir(io, entry.basename, .{});
            std.debug.print("- {s} \n", .{entry.path});
        } else if (std.mem.eql(u8, entry.basename, ".octo")) {
            std.debug.print("Found nested .octo directory : {s} \n Aborting. \n", .{entry.path});
            return OctoDirError.NestedDirs;
        }
    }
    if (octoDir == null) {
        std.debug.print("No .octo dir found. Creating one... \n", .{});
        try std.Io.Dir.cwd().createDir(io, ".octo", std.Io.File.Permissions.default_file);
        octoDir = try std.Io.Dir.cwd().openDir(io, ".octo", .{ .iterate = true });
    }
    return octoDir.?;
}
