const std = @import("std");
const OctoDirError = error{ NestedDirs, DuplicateDirs };

pub const OctoRepo = struct {
    root: []const u8,
    octo_dir: []const u8,
    allocator: std.mem.Allocator,

    pub fn init(allocator: std.mem.Allocator, root: []const u8, octo_dir: []const u8) OctoRepo {
        return .{ .root = root, .octo_dir = octo_dir, .allocator = allocator };
    }

    pub fn resolvePaths(io: std.Io, allocator: std.mem.Allocator) !struct { root: [:0]u8, octo_dir: []u8 } {
        const root = try std.Io.Dir.cwd().realPathFileAlloc(io, ".", allocator);
        const octo_dir = try std.fmt.allocPrint(allocator, "{s}/.octo", .{root});
        var dir = std.Io.Dir.cwd().openDir(io, ".octo", .{}) catch |err| switch (err) {
            error.FileNotFound => blk: {
                try std.Io.Dir.cwd().createDir(io, ".octo", std.Io.File.Permissions.default_file);
                break :blk try std.Io.Dir.cwd().openDir(io, ".octo", .{});
            },
            else => return err,
        };
        dir.close(io);
        return .{ .root = root, .octo_dir = octo_dir };
    }
    pub fn deinit(self: *OctoRepo) void {
        _ = self;
    }
};
