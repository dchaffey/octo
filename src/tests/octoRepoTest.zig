const std = @import("std");
// const OctoRepo = @import("../octoRepo.zig").OctoRepo;

test "octo repo creates .octo dir when not present" {
    const io = std.testing.io;

    // Make empty dir
    try std.Io.Dir.cwd().createDir(io, "test", std.Io.File.Permissions.default_file) catch |err| switch (err) {
        error.PathAlreadyExists => return err,
        else => return err,
    };
    const test_dir: std.Io.Dir = try std.Io.Dir.cwd().openDir(io, "test", .{ .iterate = true });
    try std.process.setCurrentDir(io, test_dir);
}
