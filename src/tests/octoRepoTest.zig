const std = @import("std");
// const OctoRepo = @import("../octoRepo.zig").OctoRepo;

test "octo repo creates .octo dir when not present" {
    const io = std.testing.io;

    // Make empty dir
    try std.Io.Dir.cwd().createDir(io, "test", std.Io.File.Permissions.default_file);
    const test_dir = std.Io.Dir.cwd().openDir(io, "test", .{});
    std.process.setCurrentDir(io, test_dir);

    try std.Io.Dir.cwd().deleteTree(io, "../test");
}
